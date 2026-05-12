"""Sandbox keeper -- holds an sbx sandbox alive across mngr invocations.

Docker Sandboxes (``sbx``) auto-stops a sandbox a few seconds after the last
``sbx exec`` returns, which is incompatible with mngr's "host stays alive
between commands" model. Empirically, the sandbox stays in the ``running``
state for the duration of any foreground ``sbx exec``. We exploit this by
running a long-lived foreground ``sbx exec`` as a background process detached
from mngr's CLI: as long as that process is alive, the sandbox stays alive.

For the *real* keeper (post-install), we run ``sshd -D`` as the foreground
command. That way:

1. sshd inside the sandbox is alive iff the keeper is alive. mngr's pyinfra
   layer can reconnect across keeper restarts without needing a separate
   "start sshd" step on every revival.
2. When the keeper dies (mngr crashed, OS reboot), the sandbox auto-stops
   and the matching sshd disappears, so we don't end up with a stale sshd
   port mapping pointing at a dead sandbox.

For the brief window during ``create_host`` before sshd is installed, the
caller spawns a *setup* keeper running ``sleep`` instead -- the sandbox needs
to be alive to install packages, but sshd doesn't exist yet.

Each sbx-managed host has at most one keeper at a time, identified by a PID
file stored alongside the host record.
"""

import errno
import os
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path
from threading import Event
from time import monotonic

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.primitives import HostId
from imbue.mngr_sbx.errors import SbxCommandError

# Sleep duration for the setup-time keeper. Any large value is fine; we
# rely on mngr-side termination, not the timeout, to end the keeper. ~365 days
# in seconds so the keeper outlives any reasonable mngr session.
_KEEPER_SLEEP_SECONDS: int = 31_536_000

# Foreground command used by the sshd-keeper. Runs sshd as PID-of-the-keeper-exec
# so killing the keeper terminates sshd too, and a revival re-launches it.
_SSHD_KEEPER_INNER_COMMAND: tuple[str, ...] = (
    "sh",
    "-c",
    "mkdir -p /run/sshd && exec /usr/sbin/sshd -D -e -o MaxSessions=100",
)


def setup_keeper_command() -> tuple[str, ...]:
    """Foreground command used during create_host while sshd is being installed."""
    return ("sleep", str(_KEEPER_SLEEP_SECONDS))


def sshd_keeper_command() -> tuple[str, ...]:
    """Foreground command used as the long-lived keeper -- runs sshd in the sandbox."""
    return _SSHD_KEEPER_INNER_COMMAND


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
    inner_command: Sequence[str],
    as_user: str | None = None,
    settle_seconds: float = 1.5,
) -> SbxKeeperHandle:
    """Start a foreground ``sbx exec`` subprocess that keeps the sandbox alive.

    The subprocess is fully detached from this Python process so the sandbox
    survives after the parent ``mngr`` invocation exits. PID and output paths
    are stored under ``<provider_dir>/keepers/`` so future mngr invocations
    can locate the keeper.

    Raises ``SbxCommandError`` if the keeper exits within the settle window
    (almost always because the sandbox is unhealthy, sshd is missing, or
    sbx itself reported an error).
    """
    keepers_dir = provider_dir / "keepers"
    keepers_dir.mkdir(parents=True, exist_ok=True)
    pid_path = keeper_pid_path(provider_dir, host_id)
    log_path = keeper_log_path(provider_dir, host_id)

    command: list[str] = ["sbx", "exec"]
    if as_user is not None:
        command.extend(["-u", as_user])
    command.append(sandbox_name)
    command.extend(inner_command)

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

    # Settle window: give sbx a moment to confirm the sandbox is up and the inner command is
    # running. If the keeper exits within this window, something is wrong (bad sandbox name,
    # missing binary, sbx error) -- bubble it up so the caller can react.
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


def kill_keeper_pid(pid: int, timeout_seconds: float = 2.0) -> None:
    """Send SIGTERM (then SIGKILL after a brief grace period) to a specific keeper PID.

    Used when we need to terminate a particular keeper without touching the pidfile -- for
    instance during the create_host handover from the setup keeper to the sshd keeper.
    sbx's CLI does not forward SIGTERM to its inner process, so SIGKILL is the common path.
    """
    if pid <= 0 or not is_keeper_alive(pid):
        return
    with log_span("Stopping sbx keeper pid={}", pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            logger.warning("Failed to SIGTERM keeper pid={}: {}", pid, e)
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


def stop_keeper(provider_dir: Path, host_id: HostId, timeout_seconds: float = 2.0) -> None:
    """Terminate the keeper subprocess recorded in the pidfile (if any) and remove the pidfile."""
    pid = read_keeper_pid(provider_dir, host_id)
    pid_path = keeper_pid_path(provider_dir, host_id)
    if pid is None:
        pid_path.unlink(missing_ok=True)
        return
    kill_keeper_pid(pid, timeout_seconds=timeout_seconds)
    pid_path.unlink(missing_ok=True)


def ensure_sshd_keeper_alive(
    provider_dir: Path,
    host_id: HostId,
    sandbox_name: str,
) -> SbxKeeperHandle:
    """Return a handle to a live sshd-keeper, spawning a new one if the existing pid is dead.

    The spawned keeper runs ``sshd -D`` inside the sandbox so SSH connectivity is restored as
    part of the revival. Callers must re-publish port 22 afterward -- sbx loses port mappings
    when the sandbox auto-stops.
    """
    existing_pid = read_keeper_pid(provider_dir, host_id)
    if existing_pid is not None and is_keeper_alive(existing_pid):
        return SbxKeeperHandle(pid=existing_pid, sandbox_name=sandbox_name)
    return spawn_keeper(
        provider_dir,
        host_id,
        sandbox_name,
        inner_command=sshd_keeper_command(),
        as_user="root",
    )
