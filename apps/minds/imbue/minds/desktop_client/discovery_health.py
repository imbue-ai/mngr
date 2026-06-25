"""App-global health watchdog for the minds discovery pipeline.

The discovery pipeline is two processes: a *producer* (``mngr observe
--discovery-only``, a grandchild of the detached ``mngr latchkey forward``
supervisor, which writes the shared discovery-events file every ~10s) and a
*consumer* (the single ``mngr forward --observe-via-file`` subprocess minds
spawns, which tails that file and folds snapshots into
``MngrCliBackendResolver``). When snapshots stop arriving the resolver freezes
at its last-known state: the workspace list, host-liveness dots, and the
freshness-gated recovery redirect all stop updating, and (in the worst case)
agent traffic stops flowing.

This watchdog detects that and tries to self-heal. It is driven by two inputs,
fed from a background loop and the consumer's lifecycle watcher respectively:

- *Producer stall* -- the producer is emitting nothing. The stall signal is the
  age of the resolver's ``last_event_at`` (its most recent discovery event of
  *any* kind), not its last *full snapshot*. A full snapshot bumps both, so
  ``last_event_at >= last_full_snapshot_at`` always; keying off events means a
  producer that is alive and emitting incremental updates but slow to complete a
  full re-poll (e.g. one provider is down) reads as *healthy*, not stalled --
  only a producer emitting literally nothing trips the watchdog. Once the
  supervisor has been seen running at least once, the watchdog also treats a
  *dead supervisor* (``remediator.is_alive()`` is False) as a stall regardless
  of timestamps, so a crash mid-session is remediated at once rather than after
  the stall timer. (The "seen running once" guard avoids racing startup, when
  the supervisor is still being brought up on a background thread and would
  momentarily read as dead.) On a stall it ``bounce``\\ es once (SIGHUP
  the observe child -- or, if the supervisor is dead, start it; gateway +
  reverse tunnels untouched), then issues repeated ``restart``\\ s (full
  supervisor restart -- fixes a wedged supervisor) on a capped exponential
  backoff. It **never gives up**: a stall keeps the app in ``RECONNECTING`` and
  keeps retrying forever (a failed ``restart`` is just another "did not help").
  Backoff-with-liveness-gating is deliberate: ``restart`` re-provisions every
  managed host, so blindly hammering a merely-slow producer would make things
  worse.
- *Consumer death* -- the consumer subprocess exited. Producer remediation
  cannot fix a dead consumer (and respawning the consumer is out of scope), so
  this transitions straight to the terminal ``BLOCKED`` tier. This is the *only*
  path to ``BLOCKED``.

The watchdog never touches the consumer subprocess: it is also the HTTP traffic
proxy, and its bound port is baked into app state / ``AgentCreator`` / the
Electron shell, so respawning it is heavyweight and risks a port rebind.

Health is a three-state machine surfaced to the chrome:

- ``HEALTHY`` -- pipeline fresh; nothing surfaced.
- ``RECONNECTING`` -- producer stall detected; remediation is healing in the
  background and keeps retrying indefinitely. The currently-loaded workspace
  still works, so nothing is surfaced (the providers panel's "time since last
  discovery" counter is the only passive signal); this state never escalates to
  the error-takeover screen.
- ``BLOCKED`` -- the consumer died: forwarding is down / the app is unusable, so
  the chrome redirects the whole app to an error-takeover screen. ``BLOCKED`` is
  terminal -- it stays until the user restarts the app -- and is reached *only*
  via consumer death, never from a producer stall.

State changes fire registered on-change callbacks, invoked outside the internal
lock so they may take the FastAPI app's own locks without deadlocking.
"""

import math
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

# How stale the resolver's ``last_event_at`` must be before the watchdog treats
# the producer as stalled. Discovery polls every ~10s (and every full snapshot
# bumps ``last_event_at``), so this is ~3-4 missed polls -- above the recovery
# redirect's own 30s freshness threshold so the two never fight over a single
# slow-but-healthy poll.
_DEFAULT_STALL_THRESHOLD_SECONDS: Final[float] = 35.0

# Base wait before the first ``restart`` (and the doubling base for the backoff
# on subsequent restarts). ~one discovery poll cycle.
_DEFAULT_REMEDIATION_WAIT_SECONDS: Final[float] = 15.0

# Ceiling for the exponential backoff between restarts. With the 15s base the
# wait grows 15 -> 30 -> 60 -> 120 -> 240 -> 300 -> 300 ... and holds at the cap.
# Keeps a never-recovering producer from idling longer than ~5 min between
# retries while still backing off hard enough not to restart-storm a slow one.
_DEFAULT_MAX_REMEDIATION_BACKOFF_SECONDS: Final[float] = 300.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveryHealth(str, Enum):
    """App-global discovery-pipeline health surfaced to the chrome."""

    HEALTHY = "healthy"
    RECONNECTING = "reconnecting"
    BLOCKED = "blocked"


class ProducerRemediator(MutableModel, ABC):
    """The producer-side remediations + liveness probe the watchdog drives.

    ``bounce`` is best-effort and MUST NOT raise: a failed bounce is treated by
    the watchdog as "did not help" and the heavier ``restart`` is tried next, so
    implementations swallow and log their own bounce errors. ``restart`` MAY
    raise ``LatchkeyError``; the watchdog catches it and treats it as "did not
    help" (it backs off and retries rather than giving up). ``is_alive`` is a
    cheap probe of whether the producer's supervisor is currently running --
    once the supervisor has been seen up at least once, a later dead reading is
    a stall the watchdog acts on immediately (before that it defers to the
    freshness timer, to avoid racing the startup thread that brings it up).
    """

    @abstractmethod
    def bounce(self) -> None:
        """SIGHUP the supervisor's ``mngr observe`` child (gateway/tunnels untouched)."""

    @abstractmethod
    def restart(self) -> None:
        """Fully restart the supervisor (bounces the gateway + reverse tunnels); may raise ``LatchkeyError``."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Whether the producer's supervisor process is currently running."""


class SupervisorProducerRemediator(ProducerRemediator):
    """A :class:`ProducerRemediator` backed by a real ``LatchkeyForwardSupervisor``.

    ``bounce`` maps to :meth:`LatchkeyForwardSupervisor.bounce` (SIGHUP the
    observe child, start-if-down), ``restart`` to
    :meth:`LatchkeyForwardSupervisor.restart` (terminate + respawn), and
    ``is_alive`` to :meth:`LatchkeyForwardSupervisor.is_running`. ``bounce``
    swallows ``LatchkeyError`` so a failed re-kick degrades to "did not help"
    and escalates; ``restart`` lets it propagate so the watchdog can log it and
    fall through to its backoff.
    """

    supervisor: LatchkeyForwardSupervisor = Field(description="The detached latchkey forward supervisor to re-kick.")

    def bounce(self) -> None:
        try:
            self.supervisor.bounce()
        except LatchkeyError as e:
            logger.warning("Discovery watchdog: producer bounce failed: {}", e)

    def restart(self) -> None:
        self.supervisor.restart()

    def is_alive(self) -> bool:
        return self.supervisor.is_running()


# No-arg, mirroring the resolver's own change callbacks: consumers re-read
# ``get_health`` rather than receiving the new tier, so a single registration
# can be shared with the resolver's on-change wake.
OnChangeCallback = Callable[[], None]


class DiscoveryHealthWatchdog(MutableModel):
    """Three-state discovery-pipeline health machine + producer remediation.

    Construct one per minds process. Drive it from two sources:

    - a background loop that calls :meth:`evaluate` every poll with the
      resolver's latest ``last_event_at``;
    - the consumer's lifecycle watcher, which calls :meth:`record_consumer_death`
      when the ``mngr forward`` subprocess exits unexpectedly.

    The chrome SSE generator subscribes via :meth:`add_on_change_callback` and
    surfaces the ``BLOCKED`` transition.
    """

    remediator: ProducerRemediator = Field(description="Producer-side bounce/restart remediations + liveness probe.")
    stall_threshold_seconds: float = Field(
        default=_DEFAULT_STALL_THRESHOLD_SECONDS,
        description="Seconds since the last discovery event before the producer is treated as stalled.",
    )
    remediation_wait_seconds: float = Field(
        default=_DEFAULT_REMEDIATION_WAIT_SECONDS,
        description="Base wait before the first restart and doubling base for the backoff on later restarts.",
    )
    max_remediation_backoff_seconds: float = Field(
        default=_DEFAULT_MAX_REMEDIATION_BACKOFF_SECONDS,
        description="Ceiling for the exponential backoff between successive restarts.",
    )
    now_fn: Callable[[], datetime] = Field(
        default=_utc_now,
        description="Injectable UTC clock (overridden in tests for deterministic timing).",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _health: DiscoveryHealth = PrivateAttr(default=DiscoveryHealth.HEALTHY)
    # When the watchdog first observed state, used as the freshness baseline for
    # the cold-start case (no event has ever arrived, so there is no
    # ``last_event_at`` to age). Set on the first ``evaluate`` call.
    _started_at: datetime | None = PrivateAttr(default=None)
    # Whether the supervisor has been seen alive at least once (sticky). The
    # liveness probe only forces a stall AFTER this latches: at startup the
    # supervisor is still being brought up on a background thread (a ``restart``
    # that deletes then rewrites the on-disk record), so a not-yet-recorded
    # supervisor reads as "dead" -- acting on that would race the startup thread
    # and risk a duplicate spawn. Until the supervisor is first seen up, the
    # freshness timer (with its own cold-start grace) governs instead.
    _supervisor_seen_alive: bool = PrivateAttr(default=False)
    # Whether the one cheap ``bounce`` has run in the current stall episode.
    # Reset on recovery; ``restart`` is only tried after ``bounce``.
    _bounce_attempted: bool = PrivateAttr(default=False)
    # How many ``restart``\\ s have run in the current stall episode -- drives the
    # exponential backoff (the next wait is ``remediation_wait_seconds *
    # 2 ** _restart_count``, capped). Reset to 0 on recovery.
    _restart_count: int = PrivateAttr(default=0)
    # When the most recent remediation ran, for the inter-remediation backoff.
    _last_remediation_at: datetime | None = PrivateAttr(default=None)
    _on_change_callbacks: list[OnChangeCallback] = PrivateAttr(default_factory=list)

    # -- Public callback registration -------------------------------------

    def add_on_change_callback(self, callback: OnChangeCallback) -> None:
        """Register a no-arg callback fired whenever the health tier changes.

        The callback takes no arguments and re-reads :meth:`get_health` (it runs
        on whichever thread caused the transition -- watchdog loop or consumer
        lifecycle watcher). Keep it fast and non-blocking.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    def remove_on_change_callback(self, callback: OnChangeCallback) -> None:
        """Unregister a previously registered change callback (no-op if absent)."""
        with self._lock:
            try:
                self._on_change_callbacks.remove(callback)
            except ValueError:
                pass

    def get_health(self) -> DiscoveryHealth:
        """Return the current discovery-pipeline health tier."""
        with self._lock:
            return self._health

    # -- Inputs -----------------------------------------------------------

    def record_consumer_death(self) -> None:
        """Transition straight to terminal ``BLOCKED`` (the consumer subprocess died).

        A dead consumer means no snapshots will ever be folded into the
        resolver again, and -- because it is also the traffic proxy -- agent
        forwarding is down. Producer remediation cannot help, so this bypasses
        the stall timer entirely. Idempotent once ``BLOCKED``.
        """
        if self._set_blocked():
            logger.error("Discovery watchdog: consumer subprocess died; discovery pipeline is down")

    def evaluate(self, last_event_at: datetime | None) -> None:
        """Re-assess producer health from the resolver's freshness and drive remediation.

        Called every poll by the watchdog loop with the resolver's most recent
        ``last_event_at`` (``None`` if no discovery event has arrived yet).
        Transitions HEALTHY <-> RECONNECTING on the stall edge and performs the
        next due producer remediation while stalled (bounce once, then restarts
        on a capped exponential backoff). The producer path never reaches
        ``BLOCKED`` -- it retries forever. A no-op once ``BLOCKED`` (consumer
        death).
        """
        now = self.now_fn()
        # Probe liveness outside the lock (it reads the supervisor's on-disk
        # record); a dead supervisor is a stall we act on regardless of the timer.
        supervisor_alive = self.remediator.is_alive()
        action: Callable[[], None] | None = None
        fire: DiscoveryHealth | None = None
        with self._lock:
            if self._health == DiscoveryHealth.BLOCKED:
                return
            if self._started_at is None:
                self._started_at = now
            if supervisor_alive:
                self._supervisor_seen_alive = True
            # Only honor a dead supervisor as an immediate stall once we've seen
            # it up at least once; before that, the startup thread is still
            # bringing it up and acting now would race it (see
            # ``_supervisor_seen_alive``). Until then, fall back to freshness.
            supervisor_dead = self._supervisor_seen_alive and not supervisor_alive
            stalled = supervisor_dead or self._is_stalled_locked(last_event_at, now)
            if not stalled:
                if self._health != DiscoveryHealth.HEALTHY:
                    self._health = DiscoveryHealth.HEALTHY
                    fire = DiscoveryHealth.HEALTHY
                self._bounce_attempted = False
                self._restart_count = 0
                self._last_remediation_at = None
            else:
                action = self._next_remediation_locked(now)
                if self._health == DiscoveryHealth.HEALTHY:
                    self._health = DiscoveryHealth.RECONNECTING
                    fire = DiscoveryHealth.RECONNECTING
        # Perform the chosen remediation and fire callbacks outside the lock.
        # Only ``restart`` may raise (``bounce`` is best-effort); a failed
        # restart is just another "did not help" -- we log it and let the
        # backoff carry us to the next attempt rather than ever giving up.
        if action is not None:
            try:
                action()
            except LatchkeyError as e:
                logger.warning("Discovery watchdog: producer restart failed; will retry with backoff: {}", e)
        if fire is not None:
            self._fire_on_change()

    # -- Internals --------------------------------------------------------

    def _is_stalled_locked(self, last_event_at: datetime | None, now: datetime) -> bool:
        """Whether the producer is emitting nothing (must hold ``_lock``).

        With an event on record, staleness is its age past
        ``stall_threshold_seconds``. With none yet (cold start), it is the time
        since the watchdog started -- so a normal startup is given the same
        grace period before the cold-start backstop fires.
        """
        if last_event_at is not None:
            age = (now - last_event_at).total_seconds()
            return age > self.stall_threshold_seconds
        baseline = self._started_at if self._started_at is not None else now
        return (now - baseline).total_seconds() > self.stall_threshold_seconds

    def _next_remediation_locked(self, now: datetime) -> Callable[[], None] | None:
        """Return the next due remediation, or ``None`` while backing off (must hold ``_lock``).

        Bounce immediately if not yet bounced this episode. Otherwise, once the
        current backoff interval has elapsed, restart (and grow the backoff).
        Never gives up: there is no terminal branch here.
        """
        if not self._bounce_attempted:
            self._bounce_attempted = True
            self._last_remediation_at = now
            return self.remediator.bounce
        if not self._backoff_elapsed_locked(now):
            return None
        self._restart_count += 1
        self._last_remediation_at = now
        return self.remediator.restart

    def _backoff_elapsed_locked(self, now: datetime) -> bool:
        """Whether the current restart-backoff interval has elapsed (must hold ``_lock``)."""
        if self._last_remediation_at is None:
            return True
        backoff = self._current_backoff_seconds()
        return (now - self._last_remediation_at).total_seconds() >= backoff

    def _current_backoff_seconds(self) -> float:
        """Current restart backoff: ``remediation_wait_seconds * 2 ** _restart_count`` capped at the ceiling.

        The watchdog retries forever, so ``_restart_count`` grows without bound;
        clamp the exponent before doubling so the power can never overflow once
        the cap is already reached (``2.0 ** 1024`` raises ``OverflowError``).
        Clamping leaves the observed schedule unchanged because any exponent at
        or above the clamp already exceeds the cap.
        """
        if self.remediation_wait_seconds <= 0.0:
            return self.max_remediation_backoff_seconds
        exponent_at_cap = math.ceil(math.log2(self.max_remediation_backoff_seconds / self.remediation_wait_seconds))
        capped_exponent = min(self._restart_count, max(exponent_at_cap, 0))
        return min(
            self.remediation_wait_seconds * (2.0**capped_exponent),
            self.max_remediation_backoff_seconds,
        )

    def _set_blocked(self) -> bool:
        """Force the terminal ``BLOCKED`` tier; return True if this call transitioned it."""
        with self._lock:
            if self._health == DiscoveryHealth.BLOCKED:
                return False
            self._health = DiscoveryHealth.BLOCKED
        self._fire_on_change()
        return True

    def _fire_on_change(self) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("Discovery watchdog on-change callback failed: {}", e)
