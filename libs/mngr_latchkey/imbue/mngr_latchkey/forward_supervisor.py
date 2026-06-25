"""Detached supervisor for the ``mngr latchkey forward`` subprocess.

Owns the on-disk record + adoption logic for a single, long-running
``mngr latchkey forward`` process. The supervisor itself is *not* the
forward subprocess -- it's a tiny in-process helper that callers (the
minds desktop client, future GUI clients) use to make sure exactly one
detached ``mngr latchkey forward`` is running for a given latchkey
directory.

Why this exists: ``mngr latchkey forward`` is the canonical owner of the
shared gateway + per-agent reverse-tunnel lifecycle. Embedders that want
the same behaviour without re-implementing :class:`LatchkeyDiscoveryHandler`
/ :class:`LatchkeyDestructionHandler` / :class:`SSHTunnelManager` wiring
can simply spawn ``mngr latchkey forward`` detached and reuse it across
embedder restarts. The detachment + adoption mechanics mirror what
:class:`Latchkey` already does for the gateway itself, so reading both
side-by-side is intentional.
"""

import os
import signal
import threading
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import psutil
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey._spawn import spawn_detached_mngr_latchkey_forward
from imbue.mngr_latchkey.core import LATCHKEY_BINARY
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.store import LatchkeyForwardInfo
from imbue.mngr_latchkey.store import delete_forward_info
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import load_forward_info
from imbue.mngr_latchkey.store import plugin_data_dir as _plugin_data_dir

# Bare-name default for the ``mngr`` CLI; callers that bundle their own
# copy (e.g. the minds desktop client) pass the absolute path explicitly.
MNGR_BINARY: Final[str] = "mngr"

# Grace period for the optional explicit-stop path. ``mngr latchkey forward``
# tears down the gateway + reverse tunnels in its SIGTERM handler, which
# can take a beat on slow systems.
_TERMINATE_GRACE_SECONDS: Final[float] = 10.0


def _mngr_argv_remainder(cmdline: list[str]) -> list[str] | None:
    """Return the literal tokens that follow the ``mngr`` argv, or ``None``.

    ``" ".join(cmdline).split()`` normalises both the one-clean-token-per-arg
    shape and the ``setproctitle``-style argv[0] overwrite that ``uv tool``'s
    entry-point wrappers do (which puts the entire joined cmdline in argv[0] and
    zeros out argv[1:], surfacing as ``["mngr latchkey forward ...", "", "", ...]``
    via :meth:`psutil.Process.cmdline`) to the same list of literal tokens. This
    also tolerates shebang rewrites (``/usr/bin/env python mngr``) and
    absolute-path invocations (``/usr/local/bin/mngr``).

    ``mngr`` is a short token, so it is matched as a whole path component
    (``mngr`` or ``*/mngr``) -- never as a substring like ``manager`` or
    ``mngr-foo``. Returns ``None`` when no ``mngr``-like token is present.
    """
    tokens = " ".join(cmdline).split()
    for idx, arg in enumerate(tokens):
        if arg == "mngr" or arg.endswith("/mngr"):
            return tokens[idx + 1 :]
    return None


def _cmdline_looks_like_mngr_latchkey_forward(cmdline: list[str]) -> bool:
    """Check whether a process's ``cmdline`` looks like our ``mngr latchkey forward``.

    Guards against PID reuse: requires the literal tokens ``latchkey`` and
    ``forward`` to appear after a ``mngr``-like argument anywhere in the argv.
    See :func:`_mngr_argv_remainder` for the cmdline-shape normalization.
    """
    remainder = _mngr_argv_remainder(cmdline)
    if remainder is None:
        return False
    return "latchkey" in remainder and "forward" in remainder


def _cmdline_looks_like_mngr_observe(cmdline: list[str]) -> bool:
    """Check whether a process's ``cmdline`` is the ``mngr observe`` discovery child.

    ``mngr latchkey forward`` spawns its discovery producer as
    ``mngr observe --discovery-only --quiet`` (see
    :class:`DiscoveryStreamConsumer`). ``--discovery-only`` is required so we
    never match an unrelated ``mngr observe`` a user runs by hand. See
    :func:`_mngr_argv_remainder` for the cmdline-shape normalization.
    """
    remainder = _mngr_argv_remainder(cmdline)
    if remainder is None:
        return False
    return "observe" in remainder and "--discovery-only" in remainder


def _forward_latchkey_directory(cmdline: list[str]) -> Path | None:
    """Extract the ``--latchkey-directory`` value from a forward's ``cmdline``.

    Returns the path the forward was launched against, or ``None`` when the flag
    is absent. Used to scope duplicate-reaping to a single latchkey directory so
    a supervisor for one profile never signals a forward for another (e.g.
    ``.minds`` vs ``.minds-staging``). Token normalization matches
    :func:`_cmdline_looks_like_mngr_latchkey_forward`, so it does not survive a
    latchkey directory containing whitespace -- an accepted limitation shared by
    that matcher (real latchkey directories never contain spaces).
    """
    tokens = " ".join(cmdline).split()
    for idx, tok in enumerate(tokens):
        if tok == "--latchkey-directory":
            if idx + 1 < len(tokens):
                return Path(tokens[idx + 1])
            return None
        if tok.startswith("--latchkey-directory="):
            return Path(tok.split("=", 1)[1])
    return None


def _resolve_or_none(path: Path) -> Path | None:
    """Resolve ``path`` for comparison, returning ``None`` if it cannot be resolved."""
    try:
        return path.resolve()
    except OSError:
        return None


def _iter_matching_forward_pids(latchkey_directory: Path) -> list[int]:
    """Return PIDs of every live ``mngr latchkey forward`` bound to ``latchkey_directory``.

    Scans the process table for processes whose cmdline both looks like our
    forward and carries a ``--latchkey-directory`` that resolves to the same
    path. The resolved-path equality is the safety boundary: only forwards for
    *this* latchkey directory are ever returned, so reaping cannot reach a
    sibling profile's supervisor.
    """
    target = _resolve_or_none(latchkey_directory)
    if target is None:
        return []
    pids: list[int] = []
    for proc in psutil.process_iter():
        try:
            cmdline = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if not _cmdline_looks_like_mngr_latchkey_forward(cmdline):
            continue
        cmd_dir = _forward_latchkey_directory(cmdline)
        if cmd_dir is None:
            continue
        if _resolve_or_none(cmd_dir) == target:
            pids.append(proc.pid)
    return pids


def _observe_child_pids(forward_pid: int) -> list[int]:
    """Return PIDs of the ``mngr observe`` discovery children under ``forward_pid``.

    Captured *before* terminating a forward so a wedged forward that has to be
    SIGKILLed (and therefore never runs its shutdown handler) does not leave its
    observe child orphaned and still writing to the shared discovery events file.
    """
    try:
        children = psutil.Process(forward_pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return []
    pids: list[int] = []
    for child in children:
        try:
            cmdline = child.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if _cmdline_looks_like_mngr_observe(cmdline):
            pids.append(child.pid)
    return pids


def is_forward_info_alive(info: LatchkeyForwardInfo) -> bool:
    """Verify that an info still corresponds to a running supervisor.

    Two checks, both must pass:

    1. A process with the recorded PID exists.
    2. That process's cmdline looks like ``mngr latchkey forward``
       (defends against PID reuse).
    """
    try:
        process = psutil.Process(info.pid)
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
        logger.info("mngr latchkey forward record is stale (pid={}): {}", info.pid, e)
        return False
    if not _cmdline_looks_like_mngr_latchkey_forward(cmdline):
        logger.warning(
            "mngr latchkey forward record points at pid {} whose cmdline does not match "
            "our pattern (expected ``mngr ... latchkey ... forward``): {!r}",
            info.pid,
            cmdline,
        )
        return False
    return True


def _terminate_pid(pid: int) -> None:
    """SIGTERM a PID, falling back to SIGKILL after a grace period.

    Silently tolerates already-dead / inaccessible / not-ours processes.
    Mirrors :func:`imbue.mngr_latchkey.core._terminate_pid` -- duplicated
    here so this module does not have to import a private helper from
    ``core.py``.
    """
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        process.terminate()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except psutil.TimeoutExpired:
        logger.warning(
            "mngr latchkey forward pid {} did not exit within grace period; sending SIGKILL",
            pid,
        )
        try:
            process.kill()
        except psutil.NoSuchProcess:
            return
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.debug("Could not terminate pid {}: {}", pid, e)


class LatchkeyForwardSupervisor(MutableModel):
    """Ensure exactly one detached ``mngr latchkey forward`` is running.

    The supervisor itself is stateless across restarts of the embedder
    -- everything it needs to reconcile lives in the
    :class:`LatchkeyForwardInfo` record under
    ``<latchkey_directory>/mngr_latchkey/``. Calling
    :meth:`ensure_running` is idempotent and safe to invoke from every
    embedder startup; concurrent calls within a single process are
    serialized via ``_lock`` so two threads cannot both decide to
    spawn.
    """

    mngr_binary: str = Field(
        default=MNGR_BINARY,
        frozen=True,
        description=(
            "Path to the ``mngr`` CLI used to launch the supervisor and (inside the "
            "supervisor) to drive ``mngr observe``. Bundled callers like the minds "
            "desktop client pass an absolute path; others fall back to ``mngr`` on PATH."
        ),
    )
    latchkey_binary: str = Field(
        default=LATCHKEY_BINARY,
        frozen=True,
        description="Path to the upstream ``latchkey`` CLI, passed to the supervisor as ``--latchkey-binary``.",
    )
    latchkey_directory: Path = Field(
        frozen=True,
        description=(
            "Root directory for ``LATCHKEY_DIRECTORY`` + the plugin's ``mngr_latchkey/`` "
            "metadata subtree. Passed to the supervisor as ``--latchkey-directory``. "
            "Also used as the location of this supervisor's own on-disk record."
        ),
    )
    cwd: Path | None = Field(
        default=None,
        frozen=True,
        description=(
            "Working directory for the spawned ``mngr latchkey forward`` process. The minds "
            "desktop client passes ``$HOME`` so the supervisor (a laptop-side ``mngr`` "
            "invocation) does not resolve project config from a transient cwd such as a dev "
            "checkout's ``.mngr/settings.toml``. ``None`` inherits the caller's cwd."
        ),
    )
    extra_env: Mapping[str, str] = Field(
        default_factory=dict,
        frozen=True,
        description=(
            "Extra environment variables to set on the spawned ``mngr latchkey forward`` "
            "process (in addition to the supervisor's own ``os.environ``). The forward "
            "process inherits these into the ``latchkey gateway`` subprocess it owns and "
            "from there into any gateway extension's ``process.env``. The minds desktop "
            "client uses this to publish the current ``LATCHKEY_EXTENSION_MINDS_API_URL`` "
            "to the bundled ``minds-api-proxy`` extension on every supervisor restart, so "
            "the proxy always points at the live Minds API port without any cross-process "
            "port-discovery dance."
        ),
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # PID of the forward child we most recently spawned (or adopted) so
    # ``stop()`` can find it even if the child has not yet published
    # its on-disk record.
    _last_known_pid: int | None = PrivateAttr(default=None)

    @property
    def plugin_data_dir(self) -> Path:
        """Return the directory the plugin owns under :attr:`latchkey_directory`."""
        return _plugin_data_dir(self.latchkey_directory)

    def get_forward_info(self) -> LatchkeyForwardInfo | None:
        """Return the persisted supervisor record, if any."""
        return load_forward_info(self.plugin_data_dir)

    def ensure_running(self) -> LatchkeyForwardInfo:
        """Spawn (or adopt) a detached ``mngr latchkey forward`` and return its info.

        Behaviour:

        * If a record exists and its PID still belongs to a process whose
          cmdline matches ``mngr latchkey forward``, the existing
          supervisor is adopted -- no new subprocess is spawned.
        * If a record exists but its PID is dead or a stranger, the
          record is deleted and a fresh supervisor is spawned.
        * If no record exists, a fresh supervisor is spawned.

        In every case, exactly one forward is left running for this
        latchkey directory: any *other* ``mngr latchkey forward`` bound to
        the same directory (a duplicate left by a prior or concurrent
        embedder instance -- the cause of multiple discovery producers
        racing on the shared events file) is reaped, along with its
        ``mngr observe`` child. Only a forward matching the live on-disk
        record is ever adopted; an unrecorded orphan is replaced rather
        than adopted, since it may be running stale code or config.

        The on-disk record is written by the spawned forward process
        itself (in :func:`_forward_command`), not by this method. The
        returned :class:`LatchkeyForwardInfo` is therefore an
        in-memory view of the spawn -- callers that need to read
        from disk should poll :func:`load_forward_info` until the
        forward child has published its record.

        ``LatchkeyError`` is raised when ``Popen`` itself fails (e.g.
        the ``mngr`` binary is missing).
        """
        plugin_dir = self.plugin_data_dir
        record_path = plugin_dir / "latchkey_forward.json"
        with self._lock:
            existing = load_forward_info(plugin_dir)
            if existing is not None and is_forward_info_alive(existing):
                # Keep the recorded supervisor; reap any other forward bound
                # to this latchkey directory (duplicates left by prior
                # instances) so a single discovery observer survives.
                self._reap_duplicate_forwards(keep_pid=existing.pid)
                logger.info(
                    "Adopted existing mngr latchkey forward supervisor (pid={}, record={})",
                    existing.pid,
                    record_path,
                )
                self._last_known_pid = existing.pid
                return existing
            if existing is None:
                logger.info(
                    "No existing mngr latchkey forward record at {}; spawning a fresh supervisor",
                    record_path,
                )
            else:
                logger.info(
                    "Discarding stale mngr latchkey forward record (pid={}, record={}); spawning fresh",
                    existing.pid,
                    record_path,
                )
                delete_forward_info(plugin_dir)

            # Reap every forward bound to this latchkey directory before
            # spawning. We have no live record to adopt, so any forward still
            # running here is an unrecorded orphan that may carry stale code or
            # config (the duplicate-producer root cause); replace it with a
            # fresh child running the current binary rather than adopting it.
            self._reap_duplicate_forwards(keep_pid=None)

            log_path = forward_log_path(plugin_dir)
            with log_span(
                "Starting detached mngr latchkey forward (log={})",
                log_path,
            ):
                try:
                    pid = spawn_detached_mngr_latchkey_forward(
                        mngr_binary=self.mngr_binary,
                        latchkey_binary=self.latchkey_binary,
                        latchkey_directory=self.latchkey_directory,
                        log_path=log_path,
                        extra_env=self.extra_env,
                        cwd=self.cwd,
                    )
                except OSError as e:
                    raise LatchkeyError(f"Failed to spawn 'mngr latchkey forward': {e}") from e

            self._last_known_pid = pid
            return LatchkeyForwardInfo(pid=pid, started_at=datetime.now(timezone.utc))

    def _reap_duplicate_forwards(self, keep_pid: int | None) -> None:
        """Terminate every ``mngr latchkey forward`` for this directory except ``keep_pid``.

        Enforces the one-forward-per-latchkey-directory invariant the discovery
        pipeline depends on (``mngr latchkey forward``'s ``mngr observe`` is the
        single producer for the shared events file). Each duplicate's observe
        child is captured before the forward is signalled and terminated after,
        so a wedged forward that must be SIGKILLed cannot leave its observe child
        orphaned and still writing snapshots. Scoped by resolved
        ``--latchkey-directory`` equality, so a sibling profile's supervisor is
        never touched. Best-effort: a duplicate that dies or is inaccessible
        mid-reap is simply skipped.
        """
        for pid in _iter_matching_forward_pids(self.latchkey_directory):
            if pid == keep_pid or pid == os.getpid():
                continue
            observe_pids = _observe_child_pids(pid)
            logger.info(
                "Reaping duplicate mngr latchkey forward (pid={}) bound to {}",
                pid,
                self.latchkey_directory,
            )
            _terminate_pid(pid)
            for observe_pid in observe_pids:
                _terminate_pid(observe_pid)

    def stop(self) -> None:
        """Terminate the supervisor and delete its record.

        SIGTERM-ing the supervisor cascades into the supervisor's own
        coupled-lifetime shutdown path: it stops the shared
        ``latchkey gateway`` subprocess, cancels every reverse tunnel,
        and exits. Embedders that want the gateway to *survive* their
        own shutdown should simply not call this method.

        Source-of-truth precedence:

        * :attr:`_last_known_pid` (set by :meth:`ensure_running`) --
          our own freshly-spawned PID. Terminated without a cmdline
          check because the freshly-forked child may not have exec'd
          its real argv yet, and any cmdline check would race the
          kernel.
        * On-disk record -- could be arbitrarily old, so the PID is
          verified via :func:`is_forward_info_alive` (PID alive +
          cmdline matches) before terminating. A record that points
          at a recycled PID never causes us to signal an unrelated
          process.
        """
        plugin_dir = self.plugin_data_dir
        with self._lock:
            cached_pid = self._last_known_pid
            self._last_known_pid = None
            info = load_forward_info(plugin_dir)
            delete_forward_info(plugin_dir)
        if cached_pid is not None:
            logger.info("Stopping detached mngr latchkey forward supervisor (pid={})", cached_pid)
            _terminate_pid(cached_pid)
            return
        if info is None:
            return
        if not is_forward_info_alive(info):
            logger.debug(
                "Skipping terminate: pid {} on disk is no longer a live mngr latchkey forward process",
                info.pid,
            )
            return
        logger.info("Stopping detached mngr latchkey forward supervisor (pid={})", info.pid)
        _terminate_pid(info.pid)

    def bounce(self) -> None:
        """Refresh the supervisor's provider set without dropping the gateway.

        If a live ``mngr latchkey forward`` is running, send it SIGHUP so it
        bounces only its ``mngr observe`` child (the shared gateway and every
        reverse tunnel stay up) and reloads the current provider set. If no
        live supervisor is found -- no record, a dead PID, or a stale record
        pointing at a stranger -- fall back to :meth:`ensure_running` so the
        bounce also brings the supervisor up (start-if-down).

        Used by the minds desktop client on every mid-session change to its
        provider set (provider enable/disable, imbue_cloud account add/remove),
        mirroring the SIGHUP it already sends its own ``mngr forward`` observe.
        """
        plugin_dir = self.plugin_data_dir
        with self._lock:
            info = load_forward_info(plugin_dir)
            live_pid = info.pid if (info is not None and is_forward_info_alive(info)) else None
        if live_pid is None:
            logger.info("No live mngr latchkey forward to bounce; ensuring one is running")
            self.ensure_running()
            return
        logger.info("Bouncing mngr latchkey forward observe via SIGHUP (pid={})", live_pid)
        try:
            os.kill(live_pid, signal.SIGHUP)
        except OSError as e:
            # The supervisor died between the liveness check and the signal.
            # Bring a fresh one up rather than leaving the provider set stale.
            logger.warning("Failed to SIGHUP mngr latchkey forward pid {}: {}; ensuring one is running", live_pid, e)
            self.ensure_running()

    def restart(self) -> LatchkeyForwardInfo:
        """Terminate any existing live supervisor and spawn a fresh one.

        Use this on embedder startup when you want to guarantee the
        supervisor was launched from the current binary's code -- i.e.
        after a package update -- rather than adopting a stale
        supervisor running an older version. The cmdline-verified
        termination in :meth:`stop` makes this safe to call
        unconditionally; a missing or stale record yields a no-op
        stop followed by a normal spawn.
        """
        self.stop()
        return self.ensure_running()
