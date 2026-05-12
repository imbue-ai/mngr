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

import threading
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
from imbue.mngr_latchkey.store import save_forward_info

# Bare-name default for the ``mngr`` CLI; callers that bundle their own
# copy (e.g. the minds desktop client) pass the absolute path explicitly.
MNGR_BINARY: Final[str] = "mngr"

# Grace period for the optional explicit-stop path. ``mngr latchkey forward``
# tears down the gateway + reverse tunnels in its SIGTERM handler, which
# can take a beat on slow systems.
_TERMINATE_GRACE_SECONDS: Final[float] = 10.0


def _cmdline_looks_like_mngr_latchkey_forward(cmdline: list[str]) -> bool:
    """Check whether a process's ``cmdline`` looks like our ``mngr latchkey forward``.

    Guards against PID reuse: requires the literal tokens ``latchkey`` and
    ``forward`` to appear after a ``mngr``-like argument anywhere in the
    argv. This tolerates shebang rewrites (``/usr/bin/env python mngr``)
    and absolute-path invocations.
    """
    if not cmdline:
        return False
    mngr_idx: int | None = None
    for idx, arg in enumerate(cmdline):
        # ``mngr`` is a short token; match it as a path component so we
        # don't fire on substrings like ``manager`` or ``mngr-foo``.
        if arg == "mngr" or arg.endswith("/mngr"):
            mngr_idx = idx
            break
    if mngr_idx is None:
        return False
    remainder = cmdline[mngr_idx + 1 :]
    return "latchkey" in remainder and "forward" in remainder


def _is_forward_info_alive(info: LatchkeyForwardInfo) -> bool:
    """Verify that an info still corresponds to our running supervisor.

    Two checks, both must pass:

    1. A process with the recorded PID exists.
    2. That process's cmdline looks like ``mngr latchkey forward``
       (defends against PID reuse).

    """
    try:
        process = psutil.Process(info.pid)
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
        logger.debug("mngr latchkey forward record is stale (pid={}): {}", info.pid, e)
        return False
    if not _cmdline_looks_like_mngr_latchkey_forward(cmdline):
        logger.debug(
            "mngr latchkey forward record points at pid {} whose cmdline is not ours: {!r}",
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

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

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

        Always returns the info corresponding to the live supervisor.
        ``LatchkeyError`` is raised when ``Popen`` itself fails (e.g.
        the ``mngr`` binary is missing).
        """
        plugin_dir = self.plugin_data_dir
        with self._lock:
            existing = load_forward_info(plugin_dir)
            if existing is not None and _is_forward_info_alive(existing):
                logger.debug(
                    "Adopted existing mngr latchkey forward supervisor (pid={})",
                    existing.pid,
                )
                return existing
            if existing is not None:
                logger.debug(
                    "Discarding stale mngr latchkey forward record (pid={})",
                    existing.pid,
                )
                delete_forward_info(plugin_dir)

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
                    )
                except OSError as e:
                    raise LatchkeyError(f"Failed to spawn 'mngr latchkey forward': {e}") from e

            info = LatchkeyForwardInfo(pid=pid, started_at=datetime.now(timezone.utc))
            save_forward_info(plugin_dir, info)
            return info

    def stop(self) -> None:
        """Terminate the supervisor and delete its record.

        SIGTERM-ing the supervisor cascades into the supervisor's own
        coupled-lifetime shutdown path: it stops the shared
        ``latchkey gateway`` subprocess, cancels every reverse tunnel,
        and exits. Embedders that want the gateway to *survive* their
        own shutdown should simply not call this method.
        """
        plugin_dir = self.plugin_data_dir
        with self._lock:
            info = load_forward_info(plugin_dir)
            delete_forward_info(plugin_dir)
        if info is not None:
            logger.info("Stopping detached mngr latchkey forward supervisor (pid={})", info.pid)
            _terminate_pid(info.pid)
