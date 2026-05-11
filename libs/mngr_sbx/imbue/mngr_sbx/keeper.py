"""Sandbox keeper -- holds an sbx sandbox alive across mngr invocations.

Docker Sandboxes (``sbx``) auto-stops a sandbox a few seconds after the last
``sbx exec`` returns, which is incompatible with mngr's "host stays alive
between commands" model. Empirically, the sandbox stays in the ``running``
state for the duration of any foreground ``sbx exec``. We exploit this by
running ``sbx exec <name> sleep infinity`` as a background process detached
from mngr's CLI: as long as that process is alive, the sandbox stays alive.

This module owns the keeper lifecycle. Each sbx-managed host has at most one
keeper, identified by a PID file stored alongside the host record.
"""

import errno
import os
import signal
import subprocess
from pathlib import Path
from threading import Event
from time import monotonic

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.primitives import HostId
from imbue.mngr_sbx.errors import SbxCommandError

# Sleep duration baked into the keeper command. Any large value is fine; we
# rely on mngr-side termination, not the timeout, to end the keeper. ~365 days
# in seconds so the keeper outlives any reasonable mngr session.
_KEEPER_SLEEP_SECONDS: int = 31_536_000


class SbxKeeperHandle(FrozenModel):
    """Lightweight record of a running keeper subprocess."""

    pid: int = Field(description="OS pid of the foreground 'sbx exec' subprocess")
    sandbox_name: str = Field(description="The sbx sandbox this keeper is attached to")


def keeper_pid_path(provider_dir: Path, host_id: HostId) -> Path:
    """Path on the local filesystem where the keeper's PID is recorded."""
    return provider_dir / "keepers" / f"{host_id}.pid"


def keeper_log_path(provider_dir: Path, host_id: HostId) -> Path:
    """Path on the local filesystem where the keeper's stdout+stderr is captured."""
    return provider_dir / "keepers" / f"{host_id}.log"


def spawn_keeper(
    provider_dir: Path,
    host_id: HostId,
    sandbox_name: str,
) -> SbxKeeperHandle:
    """Start a foreground ``sbx exec`` subprocess that keeps the sandbox alive.

    The subprocess is fully detached from this Python process so the sandbox
    survives after the parent ``mngr`` invocation exits. PID and output paths
    are stored under ``<provider_dir>/keepers/`` so future mngr invocations
    can locate the keeper.

    Raises ``SbxCommandError`` if the keeper exits within the first second
    (almost always because the sandbox name is wrong or sbx is unhealthy).
    """
    keepers_dir = provider_dir / "keepers"
    keepers_dir.mkdir(parents=True, exist_ok=True)
    pid_path = keeper_pid_path(provider_dir, host_id)
    log_path = keeper_log_path(provider_dir, host_id)

    # The sleep argument is intentionally a literal large number, not 'infinity':
    # 'sleep infinity' is a coreutils extension that may not be available in
    # minimal sbx base images.
    command = ["sbx", "exec", sandbox_name, "sleep", str(_KEEPER_SLEEP_SECONDS)]

    with log_span("Spawning sbx keeper for {}", sandbox_name):
        log_handle = log_path.open("ab", buffering=0)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    pid_path.write_text(f"{process.pid}\n")
    logger.debug("Spawned sbx keeper pid={} for sandbox {}", process.pid, sandbox_name)

    # Settle window: give sbx a moment to confirm the sandbox is up. If the
    # keeper exits immediately, the sandbox name is bad or sbx is unhealthy.
    settle_seconds = 1.5
    settle_deadline = monotonic() + settle_seconds
    poll_event = Event()
    while monotonic() < settle_deadline:
        returncode = process.poll()
        if returncode is not None:
            captured = log_path.read_text(errors="replace") if log_path.exists() else ""
            raise SbxCommandError("exec", returncode, captured.strip() or "keeper exited immediately")
        poll_event.wait(timeout=0.1)

    return SbxKeeperHandle(pid=process.pid, sandbox_name=sandbox_name)


def is_keeper_alive(pid: int) -> bool:
    """Return True if the given PID is a live process owned by the current user."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        if e.errno == errno.EPERM:
            # Process exists but we lack permission to signal it. Treat as alive.
            return True
        return False
    return True


def read_keeper_pid(provider_dir: Path, host_id: HostId) -> int | None:
    """Return the PID stored in the keeper pidfile, or None if no pidfile exists."""
    pid_path = keeper_pid_path(provider_dir, host_id)
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except ValueError:
        logger.warning("Keeper pidfile {} is malformed; ignoring", pid_path)
        return None


def stop_keeper(provider_dir: Path, host_id: HostId, timeout_seconds: float = 2.0) -> None:
    """Terminate the keeper subprocess (if any) and remove its pidfile.

    sbx's CLI does not forward SIGTERM to the inner sleep process, so SIGTERM
    alone never exits the keeper. We send SIGTERM first (best effort cleanup),
    give it a brief moment, then go straight to SIGKILL.
    """
    pid = read_keeper_pid(provider_dir, host_id)
    pid_path = keeper_pid_path(provider_dir, host_id)
    if pid is None:
        pid_path.unlink(missing_ok=True)
        return

    if not is_keeper_alive(pid):
        pid_path.unlink(missing_ok=True)
        return

    with log_span("Stopping sbx keeper pid={}", pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            logger.warning("Failed to SIGTERM keeper pid={}: {}", pid, e)
            pid_path.unlink(missing_ok=True)
            return

        deadline = monotonic() + timeout_seconds
        poll_event = Event()
        was_terminated = False
        while monotonic() < deadline:
            if not is_keeper_alive(pid):
                was_terminated = True
                break
            poll_event.wait(timeout=0.1)
        if not was_terminated:
            logger.debug("Keeper pid={} did not exit on SIGTERM after {}s; sending SIGKILL", pid, timeout_seconds)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError as e:
                logger.warning("Failed to SIGKILL keeper pid={}: {}", pid, e)

    pid_path.unlink(missing_ok=True)


def ensure_keeper_alive(
    provider_dir: Path,
    host_id: HostId,
    sandbox_name: str,
) -> SbxKeeperHandle:
    """Return a handle to a live keeper, spawning a new one if the existing pid is dead."""
    existing_pid = read_keeper_pid(provider_dir, host_id)
    if existing_pid is not None and is_keeper_alive(existing_pid):
        return SbxKeeperHandle(pid=existing_pid, sandbox_name=sandbox_name)
    return spawn_keeper(provider_dir, host_id, sandbox_name)
