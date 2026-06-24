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
from collections.abc import Collection
from collections.abc import Mapping
from enum import auto
from importlib import resources
from pathlib import Path
from typing import Final

from loguru import logger
from packaging.version import InvalidVersion
from packaging.version import Version
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey._spawn import spawn_detached_latchkey_ensure_browser
from imbue.mngr_latchkey.encryption_key import LatchkeyEncryptionKeyPermissionError
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import default_permissions_path
from imbue.mngr_latchkey.store import ensure_admin_permissions_file
from imbue.mngr_latchkey.store import ensure_browser_log_path
from imbue.mngr_latchkey.store import forward_events_log_path
from imbue.mngr_latchkey.store import plugin_data_dir as _plugin_data_dir
from imbue.mngr_latchkey.store import save_permissions

# Default value for :attr:`Latchkey.latchkey_binary` -- the bare
# command name, looked up on ``PATH`` by every spawn site via
# :func:`shutil.which` / direct ``execvp``. Callers that bundle their
# own copy of the upstream latchkey CLI (e.g. minds' Electron shell)
# pass the absolute path explicitly via ``Latchkey(latchkey_binary=...)``.
LATCHKEY_BINARY: Final[str] = "latchkey"

_DEFAULT_LISTEN_HOST: Final[str] = "127.0.0.1"

# Maximum time to wait after spawning the ``latchkey gateway`` subprocess
# for it to bind its listen port. Without this, ``_spawn_gateway`` could
# publish a fresh port to callers while the child was still in
# its startup window, and a second ``ensure_gateway_started`` caller's
# liveness probe would fail and trigger a spurious second spawn.
_GATEWAY_BIND_TIMEOUT_SECONDS: Final[float] = 10.0
_GATEWAY_BIND_POLL_INTERVAL_SECONDS: Final[float] = 0.05

# Services-info / create-jwt are normally instant but can stall on slow keychains.
# The auth-browser flow waits on a real human and is intentionally untimed.
_SERVICES_INFO_TIMEOUT_SECONDS: Final[float] = 15.0
_CREATE_JWT_TIMEOUT_SECONDS: Final[float] = 15.0

# Empirically, reencryption takes around 0.1s.
_REENCRYPT_TIMEOUT_SECONDS: Final[float] = 5.0

# ``latchkey --version`` is a print-and-exit; 5s is generous slack for
# Node-runtime startup on cold filesystems.
_VERSION_CHECK_TIMEOUT_SECONDS: Final[float] = 5.0

# Minimum version of the upstream ``latchkey`` CLI this package will
# operate against. 2.14.0 is the first release that supports GitHub git
# operations over the gateway (including permissions) which is used for backups.
LATCHKEY_MIN_VERSION: Final[str] = "2.17.1"

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

# Env-var name read by the bundled permissions extension to clamp the
# set of files it will read or write. We pin it to the plugin data dir
# so the extension can edit per-host ``latchkey_permissions.json`` files
# under ``<plugin_data_dir>/hosts/<host_id>/`` and the admin permissions
# file at the data-dir root, but cannot reach anything else on disk.
_ENV_EXTENSION_PERMISSIONS_ROOT: Final[str] = "LATCHKEY_EXTENSION_PERMISSIONS_ROOT"

# Subdirectory of ``LATCHKEY_DIRECTORY`` from which the upstream
# ``latchkey gateway`` (>= 2.9.0) loads ``.mjs`` extension files. This
# package drops its bundled ``permissions.mjs`` and
# ``permission_requests.mjs`` files there at gateway-spawn time.
_GATEWAY_EXTENSIONS_SUBDIR: Final[str] = "extensions"


class LatchkeyError(Exception):
    """Base exception for all latchkey wrapper failures."""


class LatchkeyBinaryNotFoundError(LatchkeyError, FileNotFoundError):
    """Raised when the ``latchkey`` binary is not available on PATH."""


class LatchkeyNotInitializedError(LatchkeyError, RuntimeError):
    """Raised when ``Latchkey`` is used before ``initialize()`` has been called."""


class LatchkeyJwtMintError(LatchkeyError, RuntimeError):
    """Raised when ``latchkey gateway create-jwt`` fails to produce a JWT."""


class LatchkeyVersionError(LatchkeyError, RuntimeError):
    """Raised when the installed ``latchkey`` CLI is older than :data:`LATCHKEY_MIN_VERSION`."""


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


def _is_port_listening(host: str, port: int, timeout: float) -> bool:
    """Return True if a TCP connection to ``host:port`` succeeds within ``timeout``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


def _wait_for_port_listening(host: str, port: int, timeout: float) -> bool:
    """Poll until ``host:port`` accepts TCP connections, or ``timeout`` elapses.

    Used by ``_spawn_gateway`` to make sure the freshly-spawned
    ``latchkey gateway`` has bound its port before its
    listen port is exposed via ``gateway_port`` / ``gateway_url``, so a
    user's first request after spawn does not race the port bind.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_port_listening(host, port, timeout=_GATEWAY_BIND_POLL_INTERVAL_SECONDS):
            return True
        # ``threading.Event().wait`` is the canonical interruptible
        # short sleep in this codebase (the project ratchets against
        # ``time.sleep`` as a polling primitive).
        threading.Event().wait(timeout=_GATEWAY_BIND_POLL_INTERVAL_SECONDS)
    # One last probe in case the port came up between the final sleep
    # and the deadline, so a slow CI host doesn't false-fail.
    return _is_port_listening(host, port, timeout=_GATEWAY_BIND_POLL_INTERVAL_SECONDS)


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


def _inject_encryption_key(env: dict[str, str], encryption_key: SecretStr | None) -> None:
    """Set ``LATCHKEY_ENCRYPTION_KEY`` in ``env`` from the per-env key.

    Operator's shell ``LATCHKEY_ENCRYPTION_KEY`` always wins (it's
    already in ``env`` via ``dict(os.environ)``); the per-env key only
    fills it in when the operator hasn't set one globally.
    """
    if encryption_key is None:
        return
    if env.get("LATCHKEY_ENCRYPTION_KEY"):
        return
    env["LATCHKEY_ENCRYPTION_KEY"] = encryption_key.get_secret_value()


def _build_local_latchkey_env(
    latchkey_directory: Path | None,
    *,
    encryption_key: SecretStr | None = None,
) -> dict[str, str]:
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
    _inject_encryption_key(env, encryption_key)
    return env


def _build_env_with_latchkey_directory(
    latchkey_directory: Path | None,
    *,
    encryption_key: SecretStr | None = None,
) -> dict[str, str] | None:
    """Build an env override that pins ``LATCHKEY_DIRECTORY`` for a child process.

    Returns ``None`` when no override is requested so the child inherits
    the parent environment unchanged.
    """
    if latchkey_directory is None and encryption_key is None:
        return None
    env = dict(os.environ)
    if latchkey_directory is not None:
        env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    _inject_encryption_key(env, encryption_key)
    return env


def _build_gateway_env(
    listen_host: str,
    listen_port: int,
    latchkey_directory: Path,
    permissions_config_path: Path,
    listen_password: str,
    extension_permissions_root: Path,
    encryption_key: SecretStr | None = None,
) -> dict[str, str]:
    """Build the env dict for the ``latchkey gateway`` subprocess.

    Mirrors the env shape that ``_spawn.spawn_detached_latchkey_gateway``
    used to set up. The gateway reads its listen host/port + permissions
    config path + listen password from these env vars (the upstream
    ``latchkey`` CLI exposes them as the documented gateway-config
    surface). ``extension_permissions_root`` is consumed by the bundled
    ``permissions.mjs`` extension to clamp the set of files it will
    read or write.
    """
    latchkey_directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LATCHKEY_GATEWAY_LISTEN_HOST"] = listen_host
    env["LATCHKEY_GATEWAY_LISTEN_PORT"] = str(listen_port)
    env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    env["LATCHKEY_PERMISSIONS_CONFIG"] = str(permissions_config_path)
    env["LATCHKEY_GATEWAY_LISTEN_PASSWORD"] = listen_password
    env[_ENV_EXTENSION_PERMISSIONS_ROOT] = str(extension_permissions_root)
    _inject_encryption_key(env, encryption_key)
    return env


_BUNDLED_EXTENSION_SUFFIXES: Final[tuple[str, ...]] = (".mjs", ".json")


def _materialize_bundled_extensions(latchkey_directory: Path) -> Path:
    """Copy this package's bundled gateway extensions into ``LATCHKEY_DIRECTORY/extensions/``.

    The upstream ``latchkey gateway`` (>= 2.9.0) auto-loads every
    ``.mjs`` file in this directory at startup. We also ship sibling
    ``.json`` data files (e.g. ``services.json``) that the ``.mjs``
    extensions read at request time; those are copied next to the
    ``.mjs`` files so the extensions can locate them via
    ``import.meta.url``. We rewrite the bundled files unconditionally
    on every spawn so a package upgrade always wins over a stale
    on-disk copy. The directory is created with ``mode=0o700`` because
    it shares the same trust boundary as the rest of
    ``LATCHKEY_DIRECTORY``.
    """
    extensions_dir = latchkey_directory / _GATEWAY_EXTENSIONS_SUBDIR
    extensions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_package = resources.files("imbue.mngr_latchkey.extensions")
    for entry in source_package.iterdir():
        name = entry.name
        if not any(name.endswith(suffix) for suffix in _BUNDLED_EXTENSION_SUFFIXES):
            continue
        destination = extensions_dir / name
        destination.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
    return extensions_dir


def _log_gateway_output_line(line: str, is_stdout: bool) -> None:
    """Forward one line of ``latchkey gateway`` output to the supervisor's structured log.

    :class:`ConcurrencyGroup` always pipes a child's stdout/stderr through a
    per-line callback; this plays that callback role. The gateway is a
    subprocess whose output is unstructured text we cannot emit as native JSONL
    events ourselves, so instead of teeing it into a separate, unrotated file we
    route each line through loguru at DEBUG. That folds it into the supervisor's
    own rotating, timestamped JSONL log -- the same ``make_jsonl_file_sink``
    every other mngr/minds log uses -- so gateway output is timestamped and
    size-rotated like the rest of the logs. ``mngr latchkey forward`` points
    that log at ``<plugin_data_dir>/forward_logs/events.jsonl`` so the gateway's
    (potentially chatty) output stays in one dedicated, rotated file.
    """
    del is_stdout
    logger.debug("[latchkey gateway] {}", line.rstrip("\n"))


class _RunningGateway(FrozenModel):
    """In-memory record of the live gateway subprocess for one :class:`Latchkey`.

    A single ``Latchkey`` only ever owns at most one running gateway,
    so this is stored as a private ``_running_gateway: _RunningGateway | None``
    field. ``None`` means "not running"; non-``None`` carries both the
    bound listen port (cached so idempotent :meth:`Latchkey.start_gateway`
    calls can return the port without re-deriving it from the spawned
    subprocess) and the :class:`RunningProcess` so :meth:`stop_gateway`
    can terminate the child.
    """

    port: int = Field(description="TCP port the spawned ``latchkey gateway`` subprocess bound to.")
    process: RunningProcess = Field(
        description="Owning :class:`RunningProcess` returned by the spawning :class:`ConcurrencyGroup`.",
    )

    # ``RunningProcess`` is not pydantic-native; tolerate it through
    # the model so we can keep the field properly typed without
    # falling back to ``Any``.
    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}


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
    latchkey_directory: Path = Field(
        frozen=True,
        description=(
            "Root directory for everything latchkey-related. Passed through to spawned "
            "subprocesses as ``LATCHKEY_DIRECTORY`` so the upstream ``latchkey`` CLI's "
            "credential / config files live here, and also used as the parent of the "
            "plugin's own metadata subdirectory (``mngr_latchkey/``, accessible via "
            ":attr:`plugin_data_dir`). The per-directory encryption key is also rooted "
            "here -- see :func:`load_or_create_encryption_key` and "
            ":meth:`_load_encryption_key`. Required."
        ),
    )

    # ``_running_gateway`` is the single source of truth for the
    # gateway's lifecycle: ``None`` means "not running"; non-``None``
    # carries both the bound listen port (for return-value caching
    # across idempotent :meth:`start_gateway` calls) and the
    # :class:`RunningProcess` so :meth:`stop_gateway` can SIGTERM the
    # child.
    _running_gateway: _RunningGateway | None = PrivateAttr(default=None)
    _gateway_password: str | None = PrivateAttr(default=None)
    _admin_jwt: str | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Held *only* across the slow spawn path so two concurrent
    # ``start_gateway`` callers cannot both decide to spawn a fresh
    # gateway and leak the loser's subprocess. Kept separate from
    # ``_lock`` (which is held only for short state-mutation critical
    # sections) so the slow spawn path doesn't block fast-path readers.
    _spawn_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_initialized: bool = PrivateAttr(default=False)
    _has_ensured_browser: bool = PrivateAttr(default=False)

    # -- Gateway lifecycle ---------------------------------------------------

    @property
    def plugin_data_dir(self) -> Path:
        """Return the directory the plugin owns under :attr:`latchkey_directory`.

        Always ``<latchkey_directory>/mngr_latchkey/``. The plugin writes
        all of its own files (default permissions, per-agent permissions,
        opaque handles, log files, forward-supervisor record) here so
        they cannot collide with anything the upstream ``latchkey``
        CLI chooses to put in :attr:`latchkey_directory`.
        """
        return _plugin_data_dir(self.latchkey_directory)

    def initialize(self) -> None:
        """Validate the latchkey binary.

        Runs ``latchkey --version`` and refuses to continue if the
        installed CLI is older than :data:`LATCHKEY_MIN_VERSION`. The
        check happens at ``initialize`` time (rather than at the first
        ``ensure_gateway_started`` call) so misconfiguration surfaces
        immediately, before any agent has had a chance to be told to
        use the gateway.

        There is intentionally **no** cross-process gateway-record
        reconciliation: the new ``mngr latchkey forward`` /
        :class:`LatchkeyForwardSupervisor` design guarantees at most
        one process per latchkey directory ever spawns a gateway, so
        adopting a peer's running gateway from disk would only matter
        in the abnormal-exit case where the previous forward crashed
        and left an orphan. Orphans are accepted as a rare leak (no
        reverse tunnel still points at them once the previous forward
        died, so they sit idle until ``pkill latchkey`` runs).

        Raises:
            LatchkeyBinaryNotFoundError: when the configured binary is
                not on ``PATH`` / does not exist.
            LatchkeyVersionError: when the installed binary is older
                than :data:`LATCHKEY_MIN_VERSION`.
            LatchkeyError: for other ``latchkey --version`` failures
                (non-zero exit, unparseable output, spawn error).
        """
        self._check_minimum_version()
        with self._lock:
            self._is_initialized = True

    def start_gateway(self, concurrency_group: ConcurrencyGroup) -> int:
        """Start the shared gateway and return its bound listen port.

        ``concurrency_group`` owns the gateway subprocess: when it exits
        (e.g. on ``mngr latchkey forward`` shutdown), the gateway is
        terminated as part of the group's normal cleanup. There is no
        cross-process adoption -- the only caller that ever spawns a
        gateway is ``mngr latchkey forward``, and the supervisor wrapper
        makes sure at most one such process runs per latchkey directory.

        In-process idempotent: subsequent calls observe the cached
        :class:`_RunningGateway` and return the already-bound port
        without re-spawning. Thread-safe within a single process via
        ``_spawn_lock``.

        Pair the returned port with :attr:`listen_host` to build the
        gateway URL (``http://<listen_host>:<port>``).
        """
        # Fast path: already running.
        with self._lock:
            self._require_initialized_locked()
            running = self._running_gateway
            if running is not None:
                return running.port
        plugin_dir = self.plugin_data_dir
        # Slow path: serialize spawning. Double-check after acquiring
        # the spawn lock so a concurrent caller that already spawned
        # is observed before we duplicate the work.
        with self._spawn_lock:
            with self._lock:
                running = self._running_gateway
                if running is not None:
                    return running.port
            port, process = self._spawn_gateway(concurrency_group, plugin_dir)
            with self._lock:
                self._running_gateway = _RunningGateway(port=port, process=process)
        return port

    def stop_gateway(self) -> None:
        """Terminate the gateway tracked by this :class:`Latchkey` instance.

        SIGTERMs the underlying subprocess via the tracked
        :class:`RunningProcess` and clears the in-memory state. The
        ``ConcurrencyGroup`` that owns the subprocess would also
        terminate it on its own ``__exit__``; calling ``stop_gateway``
        explicitly is the way ``mngr latchkey forward``'s signal
        handler tears the gateway down *before* the CG exits, so the
        user sees a clean log line + a deterministic order.

        Per-agent ``latchkey_permissions.json`` files are intentionally
        *not* deleted: minds does not delete other per-agent state on
        destruction either, and keeping them around means previously
        granted permissions survive desktop-client restarts and
        reboots.
        """
        with self._lock:
            running = self._running_gateway
            self._running_gateway = None
        if running is not None:
            logger.info(
                "Stopping shared Latchkey gateway ({}:{})",
                self.listen_host,
                running.port,
            )
            try:
                running.process.terminate()
            except (OSError, RuntimeError) as e:
                logger.warning("Failed to terminate Latchkey gateway cleanly: {}", e)

    @property
    def is_gateway_running(self) -> bool:
        """Whether this :class:`Latchkey` has spawned a gateway and not yet stopped it."""
        with self._lock:
            return self._running_gateway is not None

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

    def create_admin_permissions_jwt(self) -> str:
        """Mint (and cache) the JWT for the admin permissions file.

        Materializes the admin permissions file at
        :func:`admin_permissions_path` if it does not already exist
        (idempotent) and returns a JWT pointing at it. The returned
        token is what callers send in the
        ``X-Latchkey-Gateway-Permissions-Override`` header when they
        want to reach the gateway's bundled ``permissions`` /
        ``permission-requests`` extensions with admin-level
        permissions.

        Cached on the :class:`Latchkey` instance after the first
        successful mint -- subsequent calls return the same string
        without shelling out again.

        Raises:
            LatchkeyJwtMintError: if ``latchkey gateway create-jwt``
                fails (e.g. no encryption key configured).
            LatchkeyStoreError: if the admin permissions file cannot be
                materialized on disk.
        """
        with self._lock:
            cached = self._admin_jwt
        if cached is not None:
            return cached
        admin_path = ensure_admin_permissions_file(self.plugin_data_dir)
        jwt = self.create_permissions_override_jwt(admin_path)
        with self._lock:
            self._admin_jwt = jwt
        return jwt

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
        env = _build_local_latchkey_env(self.latchkey_directory, encryption_key=self._load_encryption_key())
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

    # -- Credential export ---------------------------------------------------

    def export_credentials_subset(self, destination: Path, service_names: Collection[str]) -> None:
        """Write a re-encrypted copy of the credential store, filtered to ``service_names``.

        Shells out to ``latchkey auth re-encrypt <destination> --services <service> ...``.
        ``destination`` is an output *directory* (which must already exist): the
        source store (this :class:`Latchkey`'s ``LATCHKEY_DIRECTORY``) is
        decrypted with the current per-directory encryption key and a
        re-encrypted copy containing *only* the listed services' credentials is
        written into it as ``credentials.json.enc``. The new key is read
        from the child's stdin; we pass an empty stdin (``DEVNULL``) so
        ``re-encrypt`` reuses the same encryption key, keeping the copy
        readable by the same gateway -- and the same derived password /
        permissions-override JWTs -- as the canonical store.

        ``service_names`` must be non-empty: ``--services`` requires at
        least one service, and an empty bundle is meaningless. The caller
        resolves the host's granted services (and drops the ones with no
        stored credentials) first, and handles the "nothing to ship" case
        itself rather than calling this with an empty set. The only
        credentials that ever reach a host are the ones its permissions
        allow and that are actually stored.

        Raises:
            LatchkeyError: if ``service_names`` is empty, the binary
                cannot be launched, or the ``re-encrypt`` command exits
                non-zero.
        """
        if not service_names:
            raise LatchkeyError("export_credentials_subset requires at least one service; got an empty set")
        env = _build_local_latchkey_env(self.latchkey_directory, encryption_key=self._load_encryption_key())
        # Sorted for a deterministic command line (stable logs / tests);
        # the set of services is order-independent.
        command = [self.latchkey_binary, "auth", "re-encrypt", str(destination), "--services", *sorted(service_names)]
        cg = ConcurrencyGroup(name="latchkey-reencrypt")
        try:
            with cg:
                result = cg.run_process_to_completion(
                    command=command,
                    timeout=_REENCRYPT_TIMEOUT_SECONDS,
                    is_checked_after=False,
                    env=env,
                )
        except ConcurrencyExceptionGroup as group:
            if not group.only_exception_is_instance_of(ProcessSetupError):
                raise
            raise LatchkeyError(f"Failed to launch 'latchkey auth re-encrypt': {group}") from group
        if result.returncode != 0:
            raise LatchkeyError(
                "'latchkey auth re-encrypt' exited {} writing {}: {}".format(
                    result.returncode,
                    destination,
                    result.stderr.strip() or result.stdout.strip(),
                )
            )

    # -- Service introspection -----------------------------------------------

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo:
        """Run ``latchkey services info <service>`` and return the parsed output.

        Latchkey emits pretty-printed JSON to stdout; we parse it and pull
        out ``credentialStatus``, ``authOptions``, and ``setCredentialsExample``.
        Any failure (process error, malformed output, unrecognized status
        string) yields a service info with ``CredentialStatus.UNKNOWN`` and
        empty ``auth_options``, so the caller can fall back to its legacy
        behaviour rather than wrongly assuming credentials are valid.

        When ``is_offline`` is set, ``--offline`` is passed so latchkey
        reports the *stored* credential state without any network
        validation -- enough to tell ``MISSING`` (nothing stored) from a
        present credential, which is all the credential-export filter
        needs and avoids a per-service network round-trip.
        """
        env = _build_env_with_latchkey_directory(self.latchkey_directory, encryption_key=self._load_encryption_key())
        command = [self.latchkey_binary, "services", "info", service_name]
        if is_offline:
            command.append("--offline")
        cg = ConcurrencyGroup(name="latchkey-services-info")
        try:
            with cg:
                result = cg.run_process_to_completion(
                    command=command,
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

        Some latchkey services require a one-off ``latchkey auth
        browser-prepare <service>`` step before the regular browser sign-in
        flow can run;. In such a case, we transparently run ``auth
        browser-prepare`` and retry ``auth browser`` once.
        """
        is_success, detail = self._run_latchkey_auth_command(
            log_label="auth browser",
            argv=["auth", "browser", service_name],
            service_name=service_name,
        )
        if is_success:
            return True, ""
        if "latchkey auth browser-prepare" not in detail.lower():
            return False, detail
        logger.info(
            "latchkey auth browser {} reports preparation required; running 'auth browser-prepare' and retrying",
            service_name,
        )
        is_prepared, prepare_detail = self._run_latchkey_auth_command(
            log_label="auth browser-prepare",
            argv=["auth", "browser-prepare", service_name],
            service_name=service_name,
        )
        if not is_prepared:
            return False, prepare_detail
        return self._run_latchkey_auth_command(
            log_label="auth browser",
            argv=["auth", "browser", service_name],
            service_name=service_name,
        )

    def _run_latchkey_auth_command(
        self,
        log_label: str,
        argv: list[str],
        service_name: str,
    ) -> tuple[bool, str]:
        """Run a single ``latchkey auth ...`` subcommand and translate its exit into ``(is_success, detail)``.

        ``log_label`` is the human-readable name of the subcommand
        (e.g. ``"auth browser"``, ``"auth browser-prepare"``) used in
        log lines and the generic failure-message fallback.
        """
        env = _build_env_with_latchkey_directory(self.latchkey_directory, encryption_key=self._load_encryption_key())
        cg = ConcurrencyGroup(name=f"latchkey-{log_label.replace(' ', '-')}")
        with cg:
            # No timeout: ``auth browser`` waits on a real human
            # completing the browser sign-in flow, which can take
            # arbitrarily long. ``auth browser-prepare`` is typically
            # non-interactive but may still hit the network, so we keep
            # the same untimed treatment.
            result = cg.run_process_to_completion(
                command=[self.latchkey_binary, *argv],
                timeout=None,
                is_checked_after=False,
                env=env,
            )
        if result.returncode == 0:
            logger.info("latchkey {} {} succeeded", log_label, service_name)
            return True, ""
        message = result.stderr.strip() or result.stdout.strip() or f"latchkey {log_label} failed"
        logger.warning(
            "latchkey {} {} exited {}: {}",
            log_label,
            service_name,
            result.returncode,
            message,
        )
        return False, message

    # -- Internals -----------------------------------------------------------

    def _require_initialized_locked(self) -> None:
        if not self._is_initialized:
            raise LatchkeyNotInitializedError(
                "Latchkey.initialize() must be called before use",
            )

    def _load_encryption_key(self) -> SecretStr:
        """Load (or, on first call against this directory, mint) the per-directory encryption key.

        Re-reads the on-disk key on every subprocess-spawn call rather
        than caching it on ``self`` so the secret only lives in
        parent-process memory for the duration of a single
        env-builder + process-spawn call frame. The on-disk file (and
        the spawned child's own copy of the env var) are the only
        steady-state holders.

        Re-raises :class:`LatchkeyEncryptionKeyPermissionError` as a
        :class:`LatchkeyError` so callers catching the latter (e.g.
        the ``mngr latchkey`` CLI's ``ClickException`` translator)
        get the friendly path.
        """
        try:
            return load_or_create_encryption_key(self.latchkey_directory)
        except LatchkeyEncryptionKeyPermissionError as e:
            raise LatchkeyError(str(e)) from e

    def _check_minimum_version(self) -> None:
        """Refuse to initialize if the installed latchkey CLI is too old.

        Runs ``latchkey --version`` and parses the (single-line, possibly
        ``v``-prefixed) version string with :class:`packaging.version.Version`.
        See :data:`LATCHKEY_MIN_VERSION` for the required version.
        """
        if shutil.which(self.latchkey_binary) is None and not Path(self.latchkey_binary).is_file():
            raise LatchkeyBinaryNotFoundError(f"Latchkey binary not found: {self.latchkey_binary}")

        env = _build_local_latchkey_env(self.latchkey_directory, encryption_key=self._load_encryption_key())
        cg = ConcurrencyGroup(name="latchkey-version")
        try:
            with cg:
                result = cg.run_process_to_completion(
                    command=[self.latchkey_binary, "--version"],
                    timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
                    is_checked_after=False,
                    env=env,
                )
        except ConcurrencyExceptionGroup as group:
            if not group.only_exception_is_instance_of(ProcessSetupError):
                raise
            raise LatchkeyError(f"Failed to launch 'latchkey --version': {group}") from group
        if result.returncode != 0:
            raise LatchkeyError(
                "'latchkey --version' exited {} : {}".format(
                    result.returncode,
                    result.stderr.strip() or result.stdout.strip(),
                )
            )
        raw = result.stdout.strip()
        # Tolerate an optional leading ``v`` (some CLIs print ``v2.9.0``);
        # otherwise the string must be a valid PEP 440 version.
        cleaned = raw.removeprefix("v")
        try:
            installed = Version(cleaned)
        except InvalidVersion as e:
            raise LatchkeyError(f"Could not parse 'latchkey --version' output {raw!r}: {e}") from e
        minimum = Version(LATCHKEY_MIN_VERSION)
        if installed < minimum:
            raise LatchkeyVersionError(
                f"Installed latchkey version {installed} is older than the required minimum {minimum}; "
                f"upgrade the binary at {self.latchkey_binary}."
            )

    def _spawn_gateway(
        self,
        concurrency_group: ConcurrencyGroup,
        plugin_dir: Path,
    ) -> tuple[int, RunningProcess]:
        """Spawn a fresh ``latchkey gateway`` and return its listen port + :class:`RunningProcess`.

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
        self._ensure_browser_once(plugin_dir)

        # Latchkey treats a missing permissions file as ``allow all``, so
        # we always materialize an empty-rules default file before
        # spawning the gateway. This guarantees that any request that
        # fails to attach a valid permissions-override JWT is denied for
        # every service rather than implicitly granted. Pre-existing
        # files are left untouched; minds always rewrites them with
        # empty rules anyway, but on adoption we leave the existing one
        # alone in case the user inspected it.
        default_perms = default_permissions_path(plugin_dir)
        if not default_perms.is_file():
            save_permissions(default_perms, LatchkeyPermissionsConfig())

        # Derive the password before spawning so the gateway and the
        # eventual agent-side env var agree on a value. ``derive_gateway_password``
        # is cached, so subsequent calls are free.
        try:
            password = self.derive_gateway_password()
        except LatchkeyJwtMintError as e:
            raise LatchkeyError(f"Failed to derive gateway password: {e}") from e

        # Drop the bundled gateway extensions into LATCHKEY_DIRECTORY so
        # ``latchkey gateway`` picks them up at startup. Always rewrites
        # so a package upgrade overrides any stale on-disk copy.
        _materialize_bundled_extensions(self.latchkey_directory)

        port = _allocate_free_port(self.listen_host)
        env = _build_gateway_env(
            listen_host=self.listen_host,
            listen_port=port,
            latchkey_directory=self.latchkey_directory,
            permissions_config_path=default_perms,
            listen_password=password,
            extension_permissions_root=plugin_dir,
            encryption_key=self._load_encryption_key(),
        )

        with log_span(
            "Starting shared Latchkey gateway on {}:{}",
            self.listen_host,
            port,
        ):
            try:
                process = concurrency_group.run_process_in_background(
                    command=[self.latchkey_binary, "gateway"],
                    env=env,
                    on_output=_log_gateway_output_line,
                )
            except (ConcurrencyExceptionGroup, OSError) as e:
                raise LatchkeyError(f"Failed to spawn shared Latchkey gateway: {e}") from e

            # Block until the freshly-spawned subprocess actually binds
            # its port. Returning earlier would let a caller use the
            # gateway's URL before the gateway is actually accepting
            # connections. If the gateway never comes up we terminate
            # it so the caller doesn't end up with a half-started
            # subprocess they don't know about.
            if not _wait_for_port_listening(self.listen_host, port, timeout=_GATEWAY_BIND_TIMEOUT_SECONDS):
                try:
                    process.terminate()
                except (OSError, RuntimeError) as e:
                    logger.warning("Failed to terminate half-started latchkey gateway: {}", e)
                raise LatchkeyError(
                    "Spawned latchkey gateway did not bind {}:{} within {:.1f}s; see {} for details".format(
                        self.listen_host, port, _GATEWAY_BIND_TIMEOUT_SECONDS, forward_events_log_path(plugin_dir)
                    )
                )

        return port, process

    def _ensure_browser_once(self, plugin_dir: Path) -> None:
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
        log_path = ensure_browser_log_path(plugin_dir)
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
