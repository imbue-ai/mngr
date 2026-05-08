"""Single wrapper around all interactions with the latchkey CLI.

The ``Latchkey`` class consolidates four responsibilities that all
ultimately shell out to the same upstream binary:

1. Spawning, adopting, and tracking the single shared
   ``latchkey gateway`` subprocess (one for all minds-managed agents).
2. Deriving the gateway's shared password and minting per-agent
   permissions-override JWTs via ``latchkey gateway create-jwt``.
3. Probing credential status for a service via ``latchkey services info``.
4. Launching the interactive ``latchkey auth browser`` flow when the user
   needs to authenticate.

Keeping these in one class means there is exactly one place that knows
about the binary path, the shared ``LATCHKEY_DIRECTORY``, and the global
locking concerns, and exactly one place to mock or replace when something
needs to change.
"""

import hashlib
import json
import os
import shutil
import socket
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

import psutil
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey._spawn import spawn_detached_latchkey_ensure_browser
from imbue.mngr_latchkey._spawn import spawn_detached_latchkey_gateway
from imbue.mngr_latchkey.store import LatchkeyGatewayInfo
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import default_permissions_path
from imbue.mngr_latchkey.store import delete_gateway_info
from imbue.mngr_latchkey.store import delete_legacy_per_agent_gateway_records
from imbue.mngr_latchkey.store import ensure_browser_log_path
from imbue.mngr_latchkey.store import gateway_log_path
from imbue.mngr_latchkey.store import load_gateway_info
from imbue.mngr_latchkey.store import save_gateway_info
from imbue.mngr_latchkey.store import save_permissions

LATCHKEY_BINARY: Final[str] = "latchkey"

# Env var consulted by :func:`resolve_latchkey_binary` when no explicit
# override is supplied. Lets a wrapping process (e.g. the minds Electron
# shell, which bundles its own copy of latchkey) point both the
# in-process :class:`Latchkey` API and the ``mngr latchkey`` CLI at the
# same non-PATH binary without needing a flag on every invocation.
ENV_LATCHKEY_BINARY: Final[str] = "MNGR_LATCHKEY_BINARY"


def resolve_latchkey_binary(override: str | None = None) -> str:
    """Pick the path to use for the upstream ``latchkey`` CLI.

    Resolution order:

    1. Explicit ``override`` argument (e.g. a CLI flag).
    2. ``MNGR_LATCHKEY_BINARY`` env var.
    3. ``"latchkey"`` (looked up on ``PATH`` by every spawn site via
       :func:`shutil.which` / direct ``execvp``).

    Used at every entry point that constructs a :class:`Latchkey`
    (the plugin's CLI, minds' ``_build_latchkey``, plus future callers)
    so they all agree on which binary they're talking to.
    """
    if override is not None:
        return override
    env_value = os.environ.get(ENV_LATCHKEY_BINARY)
    if env_value:
        return env_value
    return LATCHKEY_BINARY


_DEFAULT_LISTEN_HOST: Final[str] = "127.0.0.1"

_LIVENESS_CONNECT_TIMEOUT_SECONDS: Final[float] = 1.0

_TERMINATE_GRACE_SECONDS: Final[float] = 5.0

# Maximum time to wait after spawning the ``latchkey gateway`` subprocess
# for it to bind its listen port. Without this, ``_spawn_gateway`` could
# publish a fresh ``LatchkeyGatewayInfo`` while the child was still in
# its startup window, and a second ``ensure_gateway_started`` caller's
# liveness probe would fail and trigger a spurious second spawn.
_GATEWAY_BIND_TIMEOUT_SECONDS: Final[float] = 10.0
_GATEWAY_BIND_POLL_INTERVAL_SECONDS: Final[float] = 0.05

# Services-info / create-jwt are normally instant but can stall on slow keychains.
# The auth-browser flow waits on a real human and is intentionally untimed.
_SERVICES_INFO_TIMEOUT_SECONDS: Final[float] = 15.0
_CREATE_JWT_TIMEOUT_SECONDS: Final[float] = 15.0

# Fixed port that every containerized/VM/VPS agent sees on its own 127.0.0.1
# when reaching the Latchkey gateway. A per-agent SSH reverse tunnel bridges
# this to the dynamic shared-gateway port on the desktop host, so the
# ``LATCHKEY_GATEWAY`` env var injected at ``mngr create`` time can be the
# same constant URL for every agent. Matches the documented default of the
# upstream ``latchkey gateway`` CLI (``1989``).
AGENT_SIDE_LATCHKEY_PORT: Final[int] = 1989

# Sentinel path passed to ``latchkey gateway create-jwt --no-validate`` when
# deriving the gateway's password. The path itself never exists and is
# never consulted by the gateway; only the encryption-key-derived signing
# key matters here. Hashing the resulting JWT yields a stable
# password-shaped string that is ultimately a function of the user's
# Latchkey encryption key, so it survives desktop-client restarts without
# us having to persist it in plaintext.
_GATEWAY_PASSWORD_SENTINEL_PATH: Final[str] = "/__minds_gateway_password__/sentinel"


class LatchkeyError(Exception):
    """Base exception for all latchkey wrapper failures."""


class LatchkeyBinaryNotFoundError(LatchkeyError, FileNotFoundError):
    """Raised when the ``latchkey`` binary is not available on PATH."""


class LatchkeyNotInitializedError(LatchkeyError, RuntimeError):
    """Raised when ``Latchkey`` is used before ``initialize()`` has been called."""


class LatchkeyJwtMintError(LatchkeyError, RuntimeError):
    """Raised when ``latchkey gateway create-jwt`` fails to produce a JWT."""


class CredentialStatus(UpperCaseStrEnum):
    """Latchkey-reported credential state for a service.

    Mirrors detent's ``ApiCredentialStatus`` enum (``missing``, ``valid``,
    ``invalid``, ``unknown``) but normalized to the project's enum convention.
    """

    MISSING = auto()
    VALID = auto()
    INVALID = auto()
    UNKNOWN = auto()


_CREDENTIAL_STATUS_BY_LATCHKEY_VALUE: Final[dict[str, CredentialStatus]] = {
    "missing": CredentialStatus.MISSING,
    "valid": CredentialStatus.VALID,
    "invalid": CredentialStatus.INVALID,
    "unknown": CredentialStatus.UNKNOWN,
}

# Latchkey's ``authOptions`` field lists the auth flows a service supports.
# The two we currently react to are ``browser`` (interactive sign-in) and
# ``set`` (user-supplied credentials via ``latchkey auth set``). Any unknown
# values are preserved verbatim so callers can do their own forward-compat
# checks without losing information.
LATCHKEY_AUTH_OPTION_BROWSER: Final[str] = "browser"
LATCHKEY_AUTH_OPTION_SET: Final[str] = "set"


class LatchkeyServiceInfo(FrozenModel):
    """Parsed output of ``latchkey services info <service>``."""

    credential_status: CredentialStatus = Field(
        description="Credential state reported by latchkey.",
    )
    auth_options: frozenset[str] = Field(
        description=(
            "Authentication option keywords latchkey says the service supports "
            "(e.g. ``browser``, ``set``). Empty when latchkey did not report "
            "any options or its output could not be parsed."
        ),
    )
    set_credentials_example: str | None = Field(
        description=(
            "Example ``latchkey auth set`` invocation latchkey suggests for "
            "manual credential setup, or ``None`` if latchkey did not provide one."
        ),
    )


_UNKNOWN_LATCHKEY_SERVICE_INFO: Final[LatchkeyServiceInfo] = LatchkeyServiceInfo(
    credential_status=CredentialStatus.UNKNOWN,
    auth_options=frozenset(),
    set_credentials_example=None,
)


def _allocate_free_port(host: str) -> int:
    """Pick a free TCP port on ``host`` by binding to port 0 and reading it back.

    There is an inherent TOCTOU race: the chosen port could be claimed by
    another process between the time this function returns and the time
    ``latchkey gateway`` rebinds it. In practice the window is tiny and
    the desktop client is the only interested party on 127.0.0.1.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _cmdline_looks_like_latchkey_gateway(cmdline: list[str]) -> bool:
    """Check whether a process's ``cmdline`` looks like our ``latchkey gateway``.

    We require ``latchkey`` to appear as a path component anywhere in the
    argv (to tolerate shebang rewriting that injects ``env`` / ``python`` as
    argv[0]) and the literal ``gateway`` subcommand anywhere after it. This
    guards against PID reuse: an unrelated process that happens to grab the
    same PID almost certainly won't match.
    """
    if not cmdline:
        return False
    latchkey_idx: int | None = None
    for idx, arg in enumerate(cmdline):
        # Match ``latchkey`` anywhere in the arg. This handles direct
        # execution (``/usr/local/bin/latchkey``), shebang rewrites that
        # push the interpreter ahead of the script path
        # (``/usr/bin/env node /opt/latchkey/cli``), and wrappers whose
        # script path includes the word "latchkey" somewhere.
        if "latchkey" in arg:
            latchkey_idx = idx
            break
    if latchkey_idx is None:
        return False
    return "gateway" in cmdline[latchkey_idx + 1 :]


def _is_port_listening(host: str, port: int) -> bool:
    """Return True if a TCP connection to ``host:port`` succeeds within the timeout."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_LIVENESS_CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


def _wait_for_port_listening(host: str, port: int, timeout: float) -> bool:
    """Poll until ``host:port`` accepts TCP connections, or ``timeout`` elapses.

    Used by ``_spawn_gateway`` to make sure the freshly-spawned
    ``latchkey gateway`` has bound its port before its
    ``LatchkeyGatewayInfo`` is published. Without this, a second
    ``ensure_gateway_started`` caller would probe the still-binding
    port, see it as dead, and spuriously spawn a duplicate gateway
    even though the spawn lock was held correctly.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_port_listening(host, port):
            return True
        # ``threading.Event().wait`` is the canonical interruptible
        # short sleep in this codebase (the project ratchets against
        # ``time.sleep`` as a polling primitive).
        threading.Event().wait(timeout=_GATEWAY_BIND_POLL_INTERVAL_SECONDS)
    # One last probe in case the port came up between the final sleep
    # and the deadline, so a slow CI host doesn't false-fail.
    return _is_port_listening(host, port)


def _is_info_alive(info: LatchkeyGatewayInfo) -> bool:
    """Verify that an info still corresponds to our running gateway.

    Three checks, all must pass:
    1. A process with the recorded PID exists.
    2. That process's cmdline looks like ``latchkey gateway`` (not PID reuse).
    3. Something accepts TCP connections on the recorded host:port.
    """
    try:
        process = psutil.Process(info.pid)
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
        logger.debug("Latchkey gateway record is stale (pid={}): {}", info.pid, e)
        return False
    if not _cmdline_looks_like_latchkey_gateway(cmdline):
        logger.debug(
            "Latchkey gateway record points at pid {} whose cmdline is not ours: {!r}",
            info.pid,
            cmdline,
        )
        return False
    if not _is_port_listening(info.host, info.port):
        logger.debug(
            "Latchkey gateway record points at pid {} but {}:{} is not accepting connections",
            info.pid,
            info.host,
            info.port,
        )
        return False
    return True


def _terminate_pid(pid: int) -> None:
    """SIGTERM a PID, falling back to SIGKILL after a grace period.

    Silently tolerates already-dead / inaccessible / not-ours processes.
    """
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        process.terminate()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except psutil.TimeoutExpired:
        logger.warning("Latchkey gateway pid {} did not exit within grace period; sending SIGKILL", pid)
        try:
            process.kill()
        except psutil.NoSuchProcess:
            return
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.debug("Could not terminate pid {}: {}", pid, e)


def _parse_credential_status(payload: Mapping[str, object], service_name: str) -> CredentialStatus:
    """Pull ``credentialStatus`` out of ``payload``, defaulting to UNKNOWN on any oddity."""
    raw_status = payload.get("credentialStatus")
    if not isinstance(raw_status, str):
        logger.warning(
            "'latchkey services info {}' did not include a credentialStatus string",
            service_name,
        )
        return CredentialStatus.UNKNOWN
    status = _CREDENTIAL_STATUS_BY_LATCHKEY_VALUE.get(raw_status)
    if status is None:
        logger.warning(
            "Unrecognized credentialStatus {!r} from 'latchkey services info {}'",
            raw_status,
            service_name,
        )
        return CredentialStatus.UNKNOWN
    return status


def _parse_auth_options(payload: Mapping[str, object], service_name: str) -> frozenset[str]:
    """Pull ``authOptions`` out of ``payload``; missing or malformed yields an empty set."""
    raw_options = payload.get("authOptions")
    if raw_options is None:
        return frozenset()
    if not isinstance(raw_options, list) or not all(isinstance(option, str) for option in raw_options):
        logger.warning(
            "'latchkey services info {}' authOptions was not a list of strings: {!r}",
            service_name,
            raw_options,
        )
        return frozenset()
    return frozenset(option for option in raw_options if isinstance(option, str))


def _parse_set_credentials_example(payload: Mapping[str, object], service_name: str) -> str | None:
    """Pull ``setCredentialsExample`` out of ``payload``; missing/non-string yields ``None``."""
    raw_example = payload.get("setCredentialsExample")
    if raw_example is None:
        return None
    if not isinstance(raw_example, str):
        logger.warning(
            "'latchkey services info {}' setCredentialsExample was not a string: {!r}",
            service_name,
            raw_example,
        )
        return None
    return raw_example


def _build_local_latchkey_env(latchkey_directory: Path | None) -> dict[str, str]:
    """Build an env override for *local* ``latchkey`` invocations.

    ``LATCHKEY_GATEWAY`` is explicitly cleared so commands that refuse to
    run in gateway mode (e.g. ``gateway create-jwt``) work even if the
    user has the env var set in their shell. ``LATCHKEY_DIRECTORY`` is
    pinned to the same shared directory the rest of minds uses so the
    derived encryption key matches the one the gateway itself will use.
    """
    env = dict(os.environ)
    env.pop("LATCHKEY_GATEWAY", None)
    if latchkey_directory is not None:
        env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    return env


def _build_env_with_latchkey_directory(latchkey_directory: Path | None) -> dict[str, str] | None:
    """Build an env override that pins ``LATCHKEY_DIRECTORY`` for a child process.

    Returns ``None`` when no override is requested so the child inherits
    the parent environment unchanged.
    """
    if latchkey_directory is None:
        return None
    env = dict(os.environ)
    env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    return env


class Latchkey(MutableModel):
    """Wraps every interaction with the upstream ``latchkey`` CLI.

    Spawns, adopts, and tracks the single shared ``latchkey gateway``
    subprocess; derives the gateway's shared password and mints
    per-agent permissions-override JWTs via ``latchkey gateway create-jwt``;
    exposes ``services_info`` to query credential state and supported auth
    options; and ``auth_browser`` to launch the interactive sign-in flow.
    The gateway is spawned detached (``start_new_session=True`` inside
    :func:`spawn_detached_latchkey_gateway`) so it survives desktop-client
    restarts; its lifecycle is reconciled against the persisted record on
    ``initialize()``.
    """

    latchkey_binary: str = Field(default=LATCHKEY_BINARY, frozen=True, description="Path to Latchkey binary")
    listen_host: str = Field(
        default=_DEFAULT_LISTEN_HOST,
        frozen=True,
        description="Host to bind the shared gateway to",
    )
    latchkey_directory: Path | None = Field(
        default=None,
        frozen=True,
        description=(
            "Value to pass through as ``LATCHKEY_DIRECTORY`` to every spawned subprocess "
            "(gateway, services-info, auth-browser, ensure-browser, create-jwt). When set, all "
            "minds-managed latchkey calls share this credential/config directory "
            "instead of falling back to the default ``~/.latchkey``. When ``None``, "
            "latchkey uses its own default."
        ),
    )

    _data_dir: Path | None = PrivateAttr(default=None)
    _info: LatchkeyGatewayInfo | None = PrivateAttr(default=None)
    _gateway_password: str | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Held *only* across the slow spawn path so two concurrent
    # ``ensure_gateway_started`` callers cannot both decide to spawn
    # a fresh gateway and leak the loser's subprocess. Kept separate
    # from ``_lock`` (which is held only for short state-mutation
    # critical sections) so the TCP liveness probe inside the slow
    # path doesn't block fast-path readers like ``get_gateway_info``
    # or ``stop_gateway``.
    _spawn_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_initialized: bool = PrivateAttr(default=False)
    _has_ensured_browser: bool = PrivateAttr(default=False)

    # -- Gateway lifecycle ---------------------------------------------------

    def initialize(self, data_dir: Path) -> None:
        """Load the persisted gateway info from ``data_dir``, adopting it if alive.

        A dead record is removed from disk. A live, still-ours gateway is
        adopted so subsequent calls to ``ensure_gateway_started`` are
        no-ops. Any leftover per-agent gateway records from older
        minds versions are also cleaned up here (their PIDs are
        terminated best-effort) since the new architecture only uses one
        shared gateway.

        Liveness probes include a TCP connect (up to
        ``_LIVENESS_CONNECT_TIMEOUT_SECONDS``), which is why they run
        outside the lock. ``initialize()`` is only expected to be called
        once before any concurrent use, so there is no real contention here.
        """
        existing = load_gateway_info(data_dir)
        is_alive = existing is not None and _is_info_alive(existing)

        # Best-effort cleanup of legacy per-agent gateway records that an
        # older minds version might have left behind. We can't recover
        # the PIDs from the record alone here (the records were already
        # parsed inside ``delete_legacy_per_agent_gateway_records`` and
        # discarded along with the file), but the intent is just to
        # avoid stale files lying around -- leftover processes will be
        # reaped when the user's machine reboots or when the user
        # explicitly cleans them up.
        legacy_agent_ids = delete_legacy_per_agent_gateway_records(data_dir)
        if legacy_agent_ids:
            logger.info(
                "Removed {} legacy per-agent latchkey gateway record(s); minds now uses a single shared gateway",
                len(legacy_agent_ids),
            )

        with self._lock:
            if self._is_initialized:
                return
            self._data_dir = data_dir
            if is_alive and existing is not None:
                logger.info(
                    "Adopted existing shared Latchkey gateway (pid={}, {}:{})",
                    existing.pid,
                    existing.host,
                    existing.port,
                )
                self._info = existing
            elif existing is not None:
                logger.info(
                    "Discarding stale Latchkey gateway record (pid={})",
                    existing.pid,
                )
                delete_gateway_info(data_dir)
                self._info = None
            else:
                self._info = None
            self._is_initialized = True

    def ensure_gateway_started(self) -> LatchkeyGatewayInfo:
        """Start the shared gateway if it is not already running.

        Idempotent and thread-safe: concurrent callers either all see
        the existing gateway (fast path) or serialize on ``_spawn_lock``
        so exactly one of them spawns a fresh subprocess and the others
        adopt its result (slow path). Without that serialization, two
        threads racing past the initial ``_info`` check would each spawn
        a real ``latchkey gateway`` subprocess and the second write to
        ``_info`` would leak the loser's process.

        The TCP liveness probe and subprocess spawn run outside
        ``_lock`` so unrelated fast-path callers (``get_gateway_info``,
        ``stop_gateway``, ``_ensure_browser_once``) are not blocked
        for the up-to-1s probe / subprocess fork window.
        """
        # Fast path: read current state under the short lock, then
        # liveness-probe the existing gateway (if any) without
        # blocking other state accesses.
        with self._lock:
            data_dir = self._require_initialized_locked()
            existing = self._info
        if existing is not None and _is_info_alive(existing):
            return existing
        # Slow path: serialize spawning. The double-check after
        # acquiring ``_spawn_lock`` matters: while we waited for the
        # spawn lock another caller may have already spawned and
        # published a fresh gateway, in which case we adopt it and
        # do not spawn a second one.
        with self._spawn_lock:
            with self._lock:
                existing = self._info
            if existing is not None and _is_info_alive(existing):
                return existing
            info = self._spawn_gateway(data_dir)
            with self._lock:
                self._info = info
                save_gateway_info(data_dir, info)
            return info

    def stop_gateway(self) -> None:
        """Terminate the shared gateway and delete its record.

        The in-memory entry and the on-disk gateway record are removed
        atomically under the lock so no other caller can observe a
        half-torn-down state. ``_terminate_pid`` is deliberately called
        outside the lock because it can wait up to
        ``_TERMINATE_GRACE_SECONDS`` for the child to exit. Per-agent
        ``latchkey_permissions.json`` files are intentionally *not*
        deleted: minds does not delete other per-agent state on
        destruction either, and keeping them around means previously
        granted permissions survive desktop-client restarts and reboots.
        """
        with self._lock:
            data_dir = self._data_dir
            info = self._info
            self._info = None
            if data_dir is not None:
                delete_gateway_info(data_dir)
        if info is not None:
            logger.info("Stopping shared Latchkey gateway (pid={})", info.pid)
            _terminate_pid(info.pid)

    def get_gateway_info(self) -> LatchkeyGatewayInfo | None:
        """Return the shared gateway info, or ``None`` if no gateway is tracked."""
        with self._lock:
            return self._info

    # -- Password / JWT derivation ------------------------------------------

    def derive_gateway_password(self) -> str:
        """Return a stable password for the shared gateway.

        Derived by minting a permissions-override JWT for a hard-coded
        sentinel path (which is never validated, never reached, and
        never consulted by the gateway itself) and SHA-256-hashing the
        result. The derivation is purely a function of the user's
        Latchkey encryption key, so the password is stable across
        desktop-client restarts without minds having to persist it in
        plaintext anywhere.

        The same value is set as ``LATCHKEY_GATEWAY_LISTEN_PASSWORD`` on
        the spawned gateway and as ``LATCHKEY_GATEWAY_PASSWORD`` on every
        agent so the gateway accepts agent traffic.

        Cached after the first successful invocation. Raises
        ``LatchkeyJwtMintError`` if ``latchkey gateway create-jwt``
        fails (e.g. no encryption key configured).
        """
        with self._lock:
            cached = self._gateway_password
        if cached is not None:
            return cached
        sentinel_jwt = self._run_create_jwt(_GATEWAY_PASSWORD_SENTINEL_PATH)
        password = hashlib.sha256(sentinel_jwt.encode("utf-8")).hexdigest()
        with self._lock:
            self._gateway_password = password
        return password

    def create_permissions_override_jwt(self, permissions_path: Path) -> str:
        """Mint an HS256 JWT that points the gateway at ``permissions_path``.

        Wraps ``latchkey gateway create-jwt --no-validate <path>``. The
        ``--no-validate`` flag is used because the file may not exist on
        the desktop-client filesystem at JWT-mint time (it lives wherever
        the gateway can read it; for now that is the same machine, but
        the JWT itself does not depend on existence). The returned JWT
        is the value to send in ``LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE``
        / the ``X-Latchkey-Gateway-Permissions-Override`` header.

        Raises ``LatchkeyJwtMintError`` if minting fails.
        """
        return self._run_create_jwt(str(permissions_path))

    def _run_create_jwt(self, permissions_config_path: str) -> str:
        """Run ``latchkey gateway create-jwt --no-validate <path>`` and return the JWT.

        Skips the existence check (``--no-validate``) so callers can
        mint JWTs for paths the desktop-client process cannot see (and
        so we can use the password-derivation sentinel path which is
        intentionally bogus). ``LATCHKEY_GATEWAY`` is explicitly
        cleared from the child env: the upstream CLI refuses to run
        ``gateway create-jwt`` in gateway-client mode, and the user
        might have it set in their shell.
        """
        env = _build_local_latchkey_env(self.latchkey_directory)
        cg = ConcurrencyGroup(name="latchkey-create-jwt")
        try:
            with cg:
                result = cg.run_process_to_completion(
                    command=[
                        self.latchkey_binary,
                        "gateway",
                        "create-jwt",
                        "--no-validate",
                        permissions_config_path,
                    ],
                    timeout=_CREATE_JWT_TIMEOUT_SECONDS,
                    is_checked_after=False,
                    env=env,
                )
        except ConcurrencyExceptionGroup as group:
            if not group.only_exception_is_instance_of(ProcessSetupError):
                raise
            raise LatchkeyJwtMintError(f"Failed to launch 'latchkey gateway create-jwt': {group}") from group
        if result.returncode != 0:
            raise LatchkeyJwtMintError(
                "'latchkey gateway create-jwt' exited {} for {!r}: {}".format(
                    result.returncode,
                    permissions_config_path,
                    result.stderr.strip() or result.stdout.strip(),
                )
            )
        jwt = result.stdout.strip()
        if not jwt:
            raise LatchkeyJwtMintError(
                f"'latchkey gateway create-jwt' produced empty output for {permissions_config_path!r}"
            )
        return jwt

    # -- Service introspection -----------------------------------------------

    def services_info(self, service_name: str) -> LatchkeyServiceInfo:
        """Run ``latchkey services info <service>`` and return the parsed output.

        Latchkey emits pretty-printed JSON to stdout; we parse it and pull
        out ``credentialStatus``, ``authOptions``, and ``setCredentialsExample``.
        Any failure (process error, malformed output, unrecognized status
        string) yields a service info with ``CredentialStatus.UNKNOWN`` and
        empty ``auth_options``, so the caller can fall back to its legacy
        behaviour rather than wrongly assuming credentials are valid.
        """
        env = _build_env_with_latchkey_directory(self.latchkey_directory)
        cg = ConcurrencyGroup(name="latchkey-services-info")
        try:
            with cg:
                result = cg.run_process_to_completion(
                    command=[self.latchkey_binary, "services", "info", service_name],
                    timeout=_SERVICES_INFO_TIMEOUT_SECONDS,
                    is_checked_after=False,
                    env=env,
                )
        except ConcurrencyExceptionGroup as group:
            # ``ConcurrencyGroup`` wraps the underlying error (e.g. a
            # ``ProcessSetupError`` when the latchkey binary is missing /
            # unexecutable) in an exception group on context-manager exit.
            # The docstring promises any process error degrades to UNKNOWN
            # rather than raising, so callers (e.g. the request dialog
            # renderer) can fall back to legacy behaviour instead of
            # crashing. Anything that isn't a process-setup failure is
            # re-raised so genuinely unexpected bugs still surface.
            if not group.only_exception_is_instance_of(ProcessSetupError):
                raise
            logger.warning("latchkey services info {} failed to start: {}", service_name, group)
            return _UNKNOWN_LATCHKEY_SERVICE_INFO
        if result.returncode != 0:
            logger.warning(
                "latchkey services info {} exited {}: {}",
                service_name,
                result.returncode,
                result.stderr.strip(),
            )
            return _UNKNOWN_LATCHKEY_SERVICE_INFO

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.warning("Could not parse 'latchkey services info {}' output as JSON: {}", service_name, e)
            return _UNKNOWN_LATCHKEY_SERVICE_INFO

        if not isinstance(payload, dict):
            logger.warning("'latchkey services info {}' returned non-object JSON", service_name)
            return _UNKNOWN_LATCHKEY_SERVICE_INFO

        return LatchkeyServiceInfo(
            credential_status=_parse_credential_status(payload, service_name),
            auth_options=_parse_auth_options(payload, service_name),
            set_credentials_example=_parse_set_credentials_example(payload, service_name),
        )

    # -- Interactive auth ----------------------------------------------------

    def auth_browser(self, service_name: str) -> tuple[bool, str]:
        """Run ``latchkey auth browser <service>`` and report success or failure.

        Returns ``(True, "")`` on a clean exit. Any non-zero exit -- whether
        from a cancelled browser flow, network failure, or something else --
        returns ``(False, message)`` where ``message`` carries the latchkey
        stderr (or stdout, or a generic fallback).
        """
        env = _build_env_with_latchkey_directory(self.latchkey_directory)
        cg = ConcurrencyGroup(name="latchkey-auth-browser")
        with cg:
            # No timeout: this command waits on a real human completing
            # the browser sign-in flow, which can take arbitrarily long.
            result = cg.run_process_to_completion(
                command=[self.latchkey_binary, "auth", "browser", service_name],
                timeout=None,
                is_checked_after=False,
                env=env,
            )
        if result.returncode == 0:
            logger.info("latchkey auth browser {} succeeded", service_name)
            return True, ""
        message = result.stderr.strip() or result.stdout.strip() or "latchkey auth browser failed"
        logger.warning(
            "latchkey auth browser {} exited {}: {}",
            service_name,
            result.returncode,
            message,
        )
        return False, message

    # -- Internals -----------------------------------------------------------

    def _require_initialized_locked(self) -> Path:
        if not self._is_initialized or self._data_dir is None:
            raise LatchkeyNotInitializedError(
                "Latchkey.initialize(data_dir=...) must be called before use",
            )
        return self._data_dir

    def _spawn_gateway(self, data_dir: Path) -> LatchkeyGatewayInfo:
        """Build a fresh ``LatchkeyGatewayInfo`` by spawning a detached gateway.

        Materializes the deny-all default permissions file, derives the
        gateway password (so the agent-side password matches), and only
        then spawns. Does not mutate ``_info`` or persist the info -- the
        caller is responsible for committing both under the lock.
        """
        if shutil.which(self.latchkey_binary) is None and not Path(self.latchkey_binary).is_file():
            raise LatchkeyBinaryNotFoundError(f"Latchkey binary not found: {self.latchkey_binary}")

        # Fire off ``latchkey ensure-browser`` in parallel the first time we
        # actually spawn the gateway in this minds session. It runs
        # detached alongside the gateway spawn below and we don't wait for
        # it.
        self._ensure_browser_once(data_dir)

        # Latchkey treats a missing permissions file as ``allow all``, so
        # we always materialize an empty-rules default file before
        # spawning the gateway. This guarantees that any request that
        # fails to attach a valid permissions-override JWT is denied for
        # every service rather than implicitly granted. Pre-existing
        # files are left untouched; minds always rewrites them with
        # empty rules anyway, but on adoption we leave the existing one
        # alone in case the user inspected it.
        default_perms = default_permissions_path(data_dir)
        if not default_perms.is_file():
            save_permissions(default_perms, LatchkeyPermissionsConfig())

        # Derive the password before spawning so the gateway and the
        # eventual agent-side env var agree on a value. ``derive_gateway_password``
        # is cached, so subsequent calls are free.
        try:
            password = self.derive_gateway_password()
        except LatchkeyJwtMintError as e:
            raise LatchkeyError(f"Failed to derive gateway password: {e}") from e

        port = _allocate_free_port(self.listen_host)
        log_path = gateway_log_path(data_dir)

        with log_span(
            "Starting shared Latchkey gateway on {}:{}",
            self.listen_host,
            port,
        ):
            try:
                pid = spawn_detached_latchkey_gateway(
                    latchkey_binary=self.latchkey_binary,
                    listen_host=self.listen_host,
                    listen_port=port,
                    log_path=log_path,
                    latchkey_directory=self.latchkey_directory,
                    permissions_config_path=default_perms,
                    listen_password=password,
                )
            except OSError as e:
                raise LatchkeyError(f"Failed to spawn shared Latchkey gateway: {e}") from e

            # Block until the freshly-spawned subprocess actually binds
            # its port. Returning earlier would let a concurrent
            # ``ensure_gateway_started`` caller probe the not-yet-bound
            # port, conclude the gateway is dead, and spuriously spawn
            # a second one. If the gateway never comes up we tear it
            # down so the caller doesn't end up with a leaked
            # subprocess plus a misleading published record.
            if not _wait_for_port_listening(self.listen_host, port, timeout=_GATEWAY_BIND_TIMEOUT_SECONDS):
                _terminate_pid(pid)
                raise LatchkeyError(
                    "Spawned latchkey gateway (pid={}) did not bind {}:{} within {:.1f}s; see {} for details".format(
                        pid, self.listen_host, port, _GATEWAY_BIND_TIMEOUT_SECONDS, log_path
                    )
                )

        return LatchkeyGatewayInfo(
            host=self.listen_host,
            port=port,
            pid=pid,
            started_at=datetime.now(timezone.utc),
        )

    def _ensure_browser_once(self, data_dir: Path) -> None:
        """Spawn ``latchkey ensure-browser`` the first time we're asked to, per Latchkey lifetime.

        ``ensure-browser`` discovers or downloads a Playwright-compatible
        browser into the shared latchkey directory. It only needs to succeed
        once per machine, but re-running it is a cheap no-op. We call it
        once per minds session at the point we know latchkey is actually
        being used (i.e. right before spawning the gateway), fire and
        forget. Failures here are logged but must not prevent gateway spawn.
        """
        with self._lock:
            if self._has_ensured_browser:
                return
            self._has_ensured_browser = True
        log_path = ensure_browser_log_path(data_dir)
        try:
            pid = spawn_detached_latchkey_ensure_browser(
                latchkey_binary=self.latchkey_binary,
                log_path=log_path,
                latchkey_directory=self.latchkey_directory,
            )
        except OSError as e:
            logger.warning("Failed to spawn ``latchkey ensure-browser``: {}", e)
            return
        logger.info("Spawned ``latchkey ensure-browser`` (pid={}, log={})", pid, log_path)
