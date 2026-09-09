"""Detached supervisor for the ``mngr latchkey forward`` subprocess.

Holds the adoption logic for a single, long-running
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

import signal
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import psutil
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_latchkey._pre_lock_migration import migrate_pre_lock_forward
from imbue.mngr_latchkey._spawn import spawn_detached_mngr_latchkey_forward
from imbue.mngr_latchkey.core import LATCHKEY_BINARY
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.store import LatchkeyForwardOwner
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import load_forward_owner
from imbue.mngr_latchkey.store import plugin_data_dir as _plugin_data_dir
from imbue.mngr_latchkey.store import probe_forward_lock

# Bare-name default for the ``mngr`` CLI; callers that bundle their own
# copy (e.g. the minds desktop client) pass the absolute path explicitly.
MNGR_BINARY: Final[str] = "mngr"

# Grace period for the optional explicit-stop path. ``mngr latchkey forward``
# tears down the gateway + reverse tunnels in its SIGTERM handler, which
# can take a beat on slow systems.
_TERMINATE_GRACE_SECONDS: Final[float] = 10.0


# A freshly spawned forward has to start python, import, and claim the
# directory before it owns anything.
_SPAWN_OWNERSHIP_TIMEOUT_SECONDS: Final[float] = 30.0
_SPAWN_OWNERSHIP_POLL_SECONDS: Final[float] = 0.05


def _owning_forward(plugin_data_dir: Path) -> tuple[psutil.Process, LatchkeyForwardOwner] | None:
    """Return the live forward owning this directory, paired with the record it published.

    One read of the directory answers both, so a caller that needs the gateway
    port as well as the process never asks twice and never has to reconcile two
    answers. See :func:`owning_forward_process` for what makes the answer sound.
    """
    owner = probe_forward_lock(plugin_data_dir)
    if owner is None:
        return None
    try:
        return psutil.Process(owner.pid), owner
    except (psutil.NoSuchProcess, psutil.ZombieProcess) as e:
        logger.debug("Forward lock at {} names pid {}, which is gone: {}", plugin_data_dir, owner.pid, e)
        return None


def owning_forward_process(plugin_data_dir: Path) -> psutil.Process | None:
    """Return the live ``mngr latchkey forward`` that owns this directory.

    A forward holds an exclusive lock on its own directory for its whole life
    and writes its pid into it, so ownership is answered from the directory
    alone -- the lock file's location is the directory scoping, and a sibling
    profile's forward is never mistaken for this one. Whether an owner is *live*
    comes from the kernel holding that lock, never from a stored timestamp, so
    no clock adjustment can make a running forward read as absent.

    The kernel releases a lock only when its holder exits, so a held lock has a
    live owner behind it. Which pid that is comes from the record beside the
    lock, which the holder clears and rewrites just after taking it: a probe
    landing between those two waits for the new stamp rather than reporting no
    owner, and one landing before the clear still reads the departed owner's
    pid.

    A handle is returned rather than a pid so the ``(pid, create_time)`` identity
    psutil captures here travels to whatever the caller does with it: signalling
    the handle cannot reach a process that recycled the pid in between. The start
    time in that identity is one the kernel records once and never revises, so it
    keeps matching across a system clock step.

    Returns ``None`` when nothing owns the directory.
    """
    owned = _owning_forward(plugin_data_dir)
    return None if owned is None else owned[0]


def is_forward_owned_by(plugin_data_dir: Path, pid: int) -> bool:
    """Return whether the live forward owning this directory is ``pid``."""
    forward_process = owning_forward_process(plugin_data_dir)
    return forward_process is not None and forward_process.pid == pid


def _descendant_processes(forward_process: psutil.Process) -> list[psutil.Process]:
    """Return a :class:`psutil.Process` for every descendant under ``forward_process``.

    A ``mngr latchkey forward`` owns several subprocesses -- its ``mngr observe``
    discovery producer, the shared ``latchkey gateway``, and per-agent reverse
    ``ssh`` tunnels. All of them are meant to die with the forward (a healthy
    forward tears them down in its SIGTERM handler). These handles are captured
    *before* the forward is terminated so a wedged forward that has to be
    SIGKILLed -- and therefore never runs that handler -- does not leave any of
    them orphaned (the discovery child would keep polluting the shared events
    file; the gateway would keep holding its port; tunnels would linger).
    Everything returned is a descendant of *this* forward, so reaping the whole
    set never reaches an unrelated process.

    Live :class:`psutil.Process` handles are returned (not bare PIDs) so each one
    snapshots its ``(pid, create_time)`` identity here, while the descendant is
    still alive. :func:`_terminate_process` then signals that handle, so psutil's
    PID-reuse guard rejects a PID that was recycled in the (up to
    ``_TERMINATE_GRACE_SECONDS``) window between this capture and the kill -- the
    common case, since a healthy forward tears its own descendants down first.
    """
    try:
        return forward_process.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return []


def _terminate_process(process: psutil.Process) -> None:
    """SIGTERM a :class:`psutil.Process`, falling back to SIGKILL after a grace period.

    Waits for the target to actually go, so the forward
    :meth:`LatchkeyForwardSupervisor.ensure_running` spawns after a reap finds the
    ownership lock free. ``kill()`` only queues the signal, so the SIGKILL path
    waits too.

    Silently tolerates already-dead / inaccessible / not-ours processes. Because
    ``process`` carries the ``(pid, create_time)`` identity captured when it was
    constructed, psutil's PID-reuse guard raises :class:`psutil.NoSuchProcess`
    (tolerated here) rather than signalling an unrelated process that recycled the
    PID after construction -- so callers may safely retain a handle captured while
    the target was alive and terminate it later.
    """
    pid = process.pid
    try:
        process.terminate()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except psutil.TimeoutExpired:
        logger.warning(
            "mngr latchkey forward pid {} did not exit within grace period; sending SIGKILL",
            pid,
        )
        _kill_and_wait(process)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.debug("Could not terminate pid {}: {}", pid, e)


def _kill_and_wait(process: psutil.Process) -> None:
    """SIGKILL a :class:`psutil.Process` and wait for it to actually go away."""
    try:
        process.kill()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except psutil.TimeoutExpired:
        logger.warning(
            "mngr latchkey forward pid {} survived SIGKILL; anything it holds stays held",
            process.pid,
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.debug("Could not kill pid {}: {}", process.pid, e)


def _terminate_process_and_descendants(forward_process: psutil.Process) -> None:
    """Terminate a forward supervisor and every descendant it owns.

    The descendants (the ``mngr observe`` discovery child, the detached
    ``latchkey gateway``, reverse ``ssh`` tunnels) are meant to die in the
    supervisor's own SIGTERM teardown -- but a wedged supervisor that has to be
    SIGKILLed after the grace period never runs it, and the gateway (spawned
    with ``start_new_session=True``) then outlives every session. The descendant
    handles are captured before signalling and terminated after, so the PID-reuse
    guard in :func:`_terminate_process` keeps a descendant the supervisor already
    tore down from being confused with a recycled PID.
    """
    descendant_processes = _descendant_processes(forward_process)
    _terminate_process(forward_process)
    for descendant_process in descendant_processes:
        _terminate_process(descendant_process)


def _terminate_pid_and_descendants(pid: int) -> None:
    """Terminate the forward at ``pid`` and every descendant it owns.

    CLEANUP: remove with ``_pre_lock_migration``, whose migration is its only caller.

    Resolves the pid to a process here, so callers must have just established
    that it is the intended one; a caller already holding a handle keeps its
    captured identity by calling :func:`_terminate_process_and_descendants`.
    """
    try:
        forward_process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    _terminate_process_and_descendants(forward_process)


class LatchkeyForwardSupervisor(MutableModel):
    """Ensure exactly one detached ``mngr latchkey forward`` is running.

    The supervisor itself is stateless across restarts of the embedder
    -- everything it needs to reconcile is the ownership lock under
    ``<latchkey_directory>/mngr_latchkey/`` and the
    :class:`LatchkeyForwardOwner` recorded beside it. Calling
    :meth:`ensure_running` is idempotent and safe to invoke from every
    embedder startup; it and :meth:`stop` are serialized within a single
    process via ``_lock``, so two threads can neither both decide to spawn
    nor have one reap the forward the other is waiting on.
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
            "Holds the forward's ownership lock and the owner record beside it."
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

    spawn_ownership_timeout_seconds: float = Field(
        default=_SPAWN_OWNERSHIP_TIMEOUT_SECONDS,
        frozen=True,
        description=(
            "How long :meth:`ensure_running` waits for a freshly spawned forward to take "
            "ownership of the latchkey directory before giving up on it."
        ),
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def plugin_data_dir(self) -> Path:
        """Return the directory the plugin owns under :attr:`latchkey_directory`."""
        return _plugin_data_dir(self.latchkey_directory)

    def get_forward_owner(self) -> LatchkeyForwardOwner | None:
        """Return the persisted owner record, if any; it outlives the forward it names."""
        return load_forward_owner(self.plugin_data_dir)

    def ensure_running(self) -> LatchkeyForwardOwner:
        """Spawn (or adopt) a detached ``mngr latchkey forward`` and return its owner record.

        Behaviour:

        * If a forward owns this directory, it is adopted -- no new subprocess
          is spawned. Ownership and the record are one fact, so this cannot
          distinguish a forward it spawned from any other; an embedder that
          needs the *current* binary running calls :meth:`restart`, which stops
          the owner first.
        * Otherwise a fresh supervisor is spawned, and the record it publishes
          when it claims the directory is what comes back.

        In every case, exactly one forward is left running for this latchkey
        directory. Which process owns it comes from
        :func:`owning_forward_process`, which reads the identity stamped into the
        exclusive lock the forward holds for its whole life. A live owner is
        never reaped here; a forward predating that lock is the one exception,
        since it holds none and would otherwise run beside the spawn below,
        putting a second ``mngr observe`` producer on the shared events file.

        The adopt check and the spawn are not atomic across processes, so the
        child can find the directory taken and refuse it. What is waited for is
        therefore an owner rather than this child specifically, and an owner that
        turns out to be someone else's forward is adopted like any other.

        ``LatchkeyError`` is raised when ``Popen`` itself fails (e.g. the ``mngr``
        binary is missing), and when nothing owns the directory within
        :attr:`spawn_ownership_timeout_seconds` of the spawn.
        """
        plugin_dir = self.plugin_data_dir
        with self._lock:
            # CLEANUP: remove this call with ``_pre_lock_migration``. A forward
            # predating the lock holds none, so it cannot be adopted and would
            # run beside the one spawned below.
            migrate_pre_lock_forward(plugin_dir, _terminate_pid_and_descendants)
            owned = _owning_forward(plugin_dir)
            if owned is not None:
                _, owner = owned
                logger.info("Adopted the mngr latchkey forward owning {} (pid={})", plugin_dir, owner.pid)
                return owner

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
                # Captured while the child is still known to be the one just
                # spawned: the ownership wait below runs for up to
                # ``spawn_ownership_timeout_seconds``.
                try:
                    spawned_process: psutil.Process | None = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    spawned_process = None

            # A spawn that returns is not yet a forward: the child can refuse
            # the directory, or die before it claims one.
            owned, _polls, _elapsed = poll_for_value(
                lambda: _owning_forward(plugin_dir),
                timeout=self.spawn_ownership_timeout_seconds,
                poll_interval=_SPAWN_OWNERSHIP_POLL_SECONDS,
            )
            if owned is None:
                # Nothing owns the directory, and nothing else knows about the
                # child: leaving it would orphan a process holding this
                # supervisor's log and possibly its own children.
                if spawned_process is not None:
                    _terminate_process_and_descendants(spawned_process)
                raise LatchkeyError(
                    f"Spawned 'mngr latchkey forward' (pid={pid}) did not take ownership of "
                    f"{self.latchkey_directory} within {self.spawn_ownership_timeout_seconds}s; see {log_path}",
                )
            _, owner = owned
            if owner.pid != pid:
                logger.info(
                    "Another mngr latchkey forward (pid={}) claimed {} first; adopting it and dropping our child "
                    "(pid={}), which refuses a directory it does not own",
                    owner.pid,
                    plugin_dir,
                    pid,
                )
                if spawned_process is not None:
                    _terminate_process_and_descendants(spawned_process)
            return owner

    def stop(self) -> None:
        """Terminate the forward owning this latchkey directory.

        SIGTERM-ing it cascades into its own coupled-lifetime shutdown path: it
        stops the shared ``latchkey gateway`` subprocess, cancels every reverse
        tunnel, and exits. Embedders that want the gateway to *survive* their
        own shutdown should simply not call this method.

        The signalled process comes from the ownership lock, so it is the one
        holding this directory at the moment it is read -- never one that merely
        held it once. Its identity is carried in the handle, so a pid recycled
        between that read and the signal is rejected rather than killed.

        Held under ``_lock`` for the terminate as well as the read, so a forward
        another thread's :meth:`ensure_running` is still waiting on cannot be
        reaped out from under it.
        """
        with self._lock:
            forward_process = owning_forward_process(self.plugin_data_dir)
            if forward_process is None:
                logger.debug("No mngr latchkey forward owns {}; nothing to stop", self.latchkey_directory)
                return
            logger.info("Stopping detached mngr latchkey forward supervisor (pid={})", forward_process.pid)
            _terminate_process_and_descendants(forward_process)

    def bounce(self) -> None:
        """Refresh the supervisor's provider set without dropping the gateway.

        If a live, fully-started ``mngr latchkey forward`` is running, send it
        SIGHUP so it bounces only its ``mngr observe`` child (the shared gateway
        and every reverse tunnel stay up) and reloads the current provider set.
        If nothing owns this latchkey directory, fall back to
        :meth:`ensure_running` so the bounce also brings the supervisor up
        (start-if-down). A live supervisor that is still starting (its record
        has no gateway port yet) is left alone entirely: its observe child does
        not exist to be bounced, and startup reads the current provider state
        anyway.

        Used by the minds desktop client on every mid-session change to its
        provider set (provider enable/disable, imbue_cloud account add/remove),
        mirroring the SIGHUP it already sends its own ``mngr forward`` observe.
        """
        plugin_dir = self.plugin_data_dir
        with self._lock:
            owned = _owning_forward(plugin_dir)
        if owned is None:
            logger.info("No live mngr latchkey forward to bounce; ensuring one is running")
            self.ensure_running()
            return
        forward_process, owner = owned
        if owner.gateway_port is None:
            # The record's gateway port is stamped only once startup completes,
            # and until then a SIGHUP can land before the forward has installed
            # its bounce handler -- the default disposition would kill it.
            logger.info(
                "mngr latchkey forward (pid={}) is still starting; skipping the observe bounce",
                owner.pid,
            )
            return
        logger.info("Bouncing mngr latchkey forward observe via SIGHUP (pid={})", owner.pid)
        try:
            # SIGHUP's default disposition is to terminate, so a pid recycled
            # since the ownership read would be killed rather than bounced.
            forward_process.send_signal(signal.SIGHUP)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            # The supervisor died between the liveness check and the signal.
            # Bring a fresh one up rather than leaving the provider set stale.
            logger.warning("Failed to SIGHUP mngr latchkey forward pid {}: {}; ensuring one is running", owner.pid, e)
            self.ensure_running()

    def restart(self) -> LatchkeyForwardOwner:
        """Terminate any existing live supervisor and spawn a fresh one.

        Use this on embedder startup to replace a supervisor left running by an
        earlier build. The forward it stops is the one owning the directory, and
        the spawn that follows adopts whichever forward claims it first, so
        another embedder racing for the same directory can still win it. The verified
        termination in :meth:`stop` makes this safe to call
        unconditionally; an unowned directory yields a stop that signals
        nothing, followed by a normal spawn. ``stop`` waits for the owner to
        exit, so the spawn that follows finds the directory unowned.
        """
        self.stop()
        return self.ensure_running()
