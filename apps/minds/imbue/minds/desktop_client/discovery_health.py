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

- *Producer stall* -- the resolver's last full snapshot has aged past
  ``stall_threshold_seconds``. A *stale* snapshot is a clean signal that the
  pipeline itself is broken rather than that a provider is down, because a
  provider outage keeps discovery *fresh* (snapshots keep flowing with the
  failure folded into ``error_by_provider_name``). On a stall the watchdog runs
  a cheap->heavy producer ladder, with a ~one-poll wait between rungs:
  ``bounce`` (SIGHUP the supervisor's observe child; gateway + reverse tunnels
  untouched -- fixes a dead/stuck observe), then ``restart`` (full supervisor
  restart -- fixes a wedged supervisor), then give up.
- *Consumer death* -- the consumer subprocess exited. The producer ladder
  cannot fix a dead consumer (and respawning the consumer is out of scope), so
  this transitions straight to the terminal ``BLOCKED`` tier.

The watchdog never touches the consumer subprocess: it is also the HTTP traffic
proxy, and its bound port is baked into app state / ``AgentCreator`` / the
Electron shell, so respawning it is heavyweight and risks a port rebind.

Health is a three-state machine surfaced to the chrome:

- ``HEALTHY`` -- pipeline fresh; nothing surfaced.
- ``RECONNECTING`` -- producer stall detected; the ladder is healing in the
  background. The currently-loaded workspace still works, so nothing new is
  surfaced (the providers panel's "time since last discovery" counter is the
  only passive signal).
- ``BLOCKED`` -- the ladder was exhausted, the consumer died, or a cold start
  never produced a first snapshot. Forwarding is down / the app is unusable;
  the chrome redirects the whole app to an error-takeover screen. ``BLOCKED``
  is terminal: once entered it stays until the user restarts the app.

State changes fire registered on-change callbacks, invoked outside the internal
lock so they may take the FastAPI app's own locks without deadlocking.
"""

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

# How stale the last full discovery snapshot must be before the watchdog treats
# the pipeline as stalled. Discovery polls every ~10s, so this is ~3-4 missed
# polls -- above the recovery redirect's own 30s freshness threshold so the two
# never fight over a single slow-but-healthy poll.
_DEFAULT_STALL_THRESHOLD_SECONDS: Final[float] = 35.0

# How long the watchdog waits after performing a ladder rung for freshness to
# return before escalating to the next rung. ~one discovery poll cycle.
_DEFAULT_RUNG_WAIT_SECONDS: Final[float] = 15.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveryHealth(str, Enum):
    """App-global discovery-pipeline health surfaced to the chrome."""

    HEALTHY = "healthy"
    RECONNECTING = "reconnecting"
    BLOCKED = "blocked"


class ProducerRemediator(MutableModel, ABC):
    """The producer-side restart ladder primitives the watchdog drives.

    Two rungs, cheap -> heavy. Implementations MUST NOT raise: a rung that
    fails is treated by the watchdog as "did not help" (the next freshness
    check decides whether to escalate), so concrete implementations swallow and
    log their own backend errors.
    """

    @abstractmethod
    def bounce(self) -> None:
        """SIGHUP the supervisor's ``mngr observe`` child (gateway/tunnels untouched)."""

    @abstractmethod
    def restart(self) -> None:
        """Fully restart the supervisor (bounces the gateway + reverse tunnels)."""


class SupervisorProducerRemediator(ProducerRemediator):
    """A :class:`ProducerRemediator` backed by a real ``LatchkeyForwardSupervisor``.

    ``bounce`` maps to :meth:`LatchkeyForwardSupervisor.bounce` (SIGHUP the
    observe child, start-if-down) and ``restart`` to
    :meth:`LatchkeyForwardSupervisor.restart` (terminate + respawn). Both
    swallow ``LatchkeyError`` so a failed remediation degrades to "did not
    help" rather than crashing the watchdog loop.
    """

    supervisor: LatchkeyForwardSupervisor = Field(description="The detached latchkey forward supervisor to re-kick.")

    def bounce(self) -> None:
        try:
            self.supervisor.bounce()
        except LatchkeyError as e:
            logger.warning("Discovery watchdog: producer bounce failed: {}", e)

    def restart(self) -> None:
        try:
            self.supervisor.restart()
        except LatchkeyError as e:
            logger.warning("Discovery watchdog: producer restart failed: {}", e)


# No-arg, mirroring the resolver's own change callbacks: consumers re-read
# ``get_health`` rather than receiving the new tier, so a single registration
# can be shared with the resolver's on-change wake.
OnChangeCallback = Callable[[], None]


class DiscoveryHealthWatchdog(MutableModel):
    """Three-state discovery-pipeline health machine + producer-restart ladder.

    Construct one per minds process. Drive it from two sources:

    - a background loop that calls :meth:`evaluate` every poll with the
      resolver's latest ``last_full_snapshot_at``;
    - the consumer's lifecycle watcher, which calls :meth:`record_consumer_death`
      when the ``mngr forward`` subprocess exits unexpectedly.

    The chrome SSE generator subscribes via :meth:`add_on_change_callback` and
    surfaces the ``BLOCKED`` transition.
    """

    remediator: ProducerRemediator = Field(description="Producer-side bounce/restart ladder primitives.")
    stall_threshold_seconds: float = Field(
        default=_DEFAULT_STALL_THRESHOLD_SECONDS,
        description="Seconds since the last full snapshot before the pipeline is treated as stalled.",
    )
    rung_wait_seconds: float = Field(
        default=_DEFAULT_RUNG_WAIT_SECONDS,
        description="Seconds to wait after a ladder rung for freshness to return before escalating.",
    )
    now_fn: Callable[[], datetime] = Field(
        default=_utc_now,
        description="Injectable UTC clock (overridden in tests for deterministic timing).",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _health: DiscoveryHealth = PrivateAttr(default=DiscoveryHealth.HEALTHY)
    # When the watchdog first observed state, used as the freshness baseline for
    # the cold-start case (no snapshot has ever arrived, so there is no
    # ``last_full_snapshot_at`` to age). Set on the first ``evaluate`` call.
    _started_at: datetime | None = PrivateAttr(default=None)
    # How many ladder rungs have run in the current stall: 0 = none, 1 =
    # bounced, 2 = restarted. Reset to 0 on recovery.
    _rungs_attempted: int = PrivateAttr(default=0)
    # When the most recent ladder rung ran, for the inter-rung wait.
    _last_rung_at: datetime | None = PrivateAttr(default=None)
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
        forwarding is down. The producer ladder cannot help, so this bypasses
        the stall timer entirely. Idempotent once ``BLOCKED``.
        """
        if self._set_blocked():
            logger.error("Discovery watchdog: consumer subprocess died; discovery pipeline is down")

    def evaluate(self, last_full_snapshot_at: datetime | None) -> None:
        """Re-assess pipeline health from the resolver's freshness and drive the ladder.

        Called every poll by the watchdog loop with the resolver's most recent
        ``last_full_snapshot_at`` (``None`` if no snapshot has arrived yet).
        Transitions HEALTHY <-> RECONNECTING on the stall edge, performs the
        next due ladder rung while stalled, and escalates to terminal
        ``BLOCKED`` once the ladder is exhausted. A no-op once ``BLOCKED``.
        """
        now = self.now_fn()
        action: Callable[[], None] | None = None
        fire: DiscoveryHealth | None = None
        with self._lock:
            if self._health == DiscoveryHealth.BLOCKED:
                return
            if self._started_at is None:
                self._started_at = now
            if not self._is_stalled_locked(last_full_snapshot_at, now):
                if self._health != DiscoveryHealth.HEALTHY:
                    self._health = DiscoveryHealth.HEALTHY
                    fire = DiscoveryHealth.HEALTHY
                self._rungs_attempted = 0
                self._last_rung_at = None
            else:
                if self._health == DiscoveryHealth.HEALTHY:
                    self._health = DiscoveryHealth.RECONNECTING
                    fire = DiscoveryHealth.RECONNECTING
                action, fire_from_ladder = self._advance_ladder_locked(now)
                if fire_from_ladder is not None:
                    fire = fire_from_ladder
        # Perform the chosen remediation and fire callbacks outside the lock.
        if action is not None:
            action()
        if fire is not None:
            self._fire_on_change()

    # -- Internals --------------------------------------------------------

    def _is_stalled_locked(self, last_full_snapshot_at: datetime | None, now: datetime) -> bool:
        """Whether the pipeline is stalled (must hold ``_lock``).

        With a snapshot on record, staleness is its age past
        ``stall_threshold_seconds``. With none yet (cold start), it is the time
        since the watchdog started -- so a normal startup is given the same
        grace period before the cold-start backstop fires.
        """
        if last_full_snapshot_at is not None:
            age = (now - last_full_snapshot_at).total_seconds()
            return age > self.stall_threshold_seconds
        baseline = self._started_at if self._started_at is not None else now
        return (now - baseline).total_seconds() > self.stall_threshold_seconds

    def _advance_ladder_locked(self, now: datetime) -> tuple[Callable[[], None] | None, DiscoveryHealth | None]:
        """Return the next due ladder action and any health transition (must hold ``_lock``).

        Rung 0 -> bounce immediately. Rung 1 -> restart once ``rung_wait_seconds``
        has elapsed since the bounce. Rung 2 -> give up (transition to
        ``BLOCKED``) once ``rung_wait_seconds`` has elapsed since the restart.
        Returns ``(action, fire)`` where either may be ``None`` (e.g. waiting
        out the inter-rung interval).
        """
        if self._rungs_attempted == 0:
            self._rungs_attempted = 1
            self._last_rung_at = now
            return self.remediator.bounce, None
        if not self._rung_wait_elapsed_locked(now):
            return None, None
        if self._rungs_attempted == 1:
            self._rungs_attempted = 2
            self._last_rung_at = now
            return self.remediator.restart, None
        self._health = DiscoveryHealth.BLOCKED
        return None, DiscoveryHealth.BLOCKED

    def _rung_wait_elapsed_locked(self, now: datetime) -> bool:
        """Whether ``rung_wait_seconds`` has elapsed since the last rung (must hold ``_lock``)."""
        if self._last_rung_at is None:
            return True
        return (now - self._last_rung_at).total_seconds() >= self.rung_wait_seconds

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
