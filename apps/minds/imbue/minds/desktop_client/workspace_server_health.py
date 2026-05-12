"""Tracks per-agent workspace-server health for restart-recovery UX.

The plugin (``mngr_forward``) emits a ``workspace_backend_failure`` envelope
each time it observes a backend failure (connect error, mid-SSE EOF, 5xx
response). Minds routes those into ``record_failure``; an HTTP 200 probe
hit from the background probe loop calls ``record_success``. The tracker
applies a simple state machine:

- HEALTHY -> STUCK: a continuous run of failures lasting at least
  ``stuck_threshold_seconds`` with no intervening success. The chrome
  titlebar reacts by navigating the content view to the recovery page.
- STUCK -> RESTARTING: the restart endpoint marks the tracker so the
  recovery page can render a different label and the probe loop keeps polling.
- {STUCK, RESTARTING} -> HEALTHY: a successful probe.

State changes fire registered on-change callbacks. Callbacks are invoked
outside the internal lock so they may take the FastAPI app's own locks
without deadlocking.

The 5-second STUCK transition is driven by a one-shot ``threading.Timer``
started on the first failure. This means a single failed request followed
by no further traffic still produces a STUCK transition after the window
elapses; the alternative (re-checking on each subsequent failure) would
leave a chrome session that emits one bad request and then idles wedged
indefinitely with no UI indication.
"""

import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId

_DEFAULT_STUCK_THRESHOLD_SECONDS: Final[float] = 5.0


class AgentHealth(str, Enum):
    """Per-agent health classification used by the tracker + chrome SSE."""

    HEALTHY = "healthy"
    STUCK = "stuck"
    RESTARTING = "restarting"


OnChangeCallback = Callable[[AgentId, AgentHealth], None]


class _AgentRecord(MutableModel):
    """Per-agent mutable state owned by the tracker. Not exposed to callers."""

    health: AgentHealth = Field(default=AgentHealth.HEALTHY)
    first_failure_at: float | None = Field(
        default=None,
        description="time.monotonic() of the first failure in the current failing run, or None.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class WorkspaceServerHealthTracker(MutableModel):
    """Per-agent health state machine driven by failure / success events.

    Construct one per minds process; share with envelope-consumer callbacks
    (which call ``record_failure``), the background probe loop (which calls
    ``record_success`` / ``record_failure``), and the chrome SSE generator
    (which subscribes via ``add_on_change_callback``).
    """

    stuck_threshold_seconds: float = Field(
        default=_DEFAULT_STUCK_THRESHOLD_SECONDS,
        description="Seconds of continuous failures before HEALTHY -> STUCK fires.",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _records: dict[str, _AgentRecord] = PrivateAttr(default_factory=dict)
    _stuck_timers: dict[str, threading.Timer] = PrivateAttr(default_factory=dict)
    _on_change_callbacks: list[OnChangeCallback] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # -- Public callback registration -------------------------------------

    def add_on_change_callback(self, callback: OnChangeCallback) -> None:
        """Register a callback fired whenever any agent's health changes.

        Callbacks receive ``(agent_id, new_health)`` and run on whichever
        thread caused the transition (envelope reader, probe loop, restart
        endpoint, or the stuck-threshold timer). Callbacks must be fast and
        non-blocking; do real work on a queue or worker thread.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    def remove_on_change_callback(self, callback: OnChangeCallback) -> None:
        """Unregister a previously registered change callback.

        Safe to call even if the callback is not currently registered (no-op).
        """
        with self._lock:
            try:
                self._on_change_callbacks.remove(callback)
            except ValueError:
                pass

    # -- State updates ----------------------------------------------------

    def record_failure(self, agent_id: AgentId) -> None:
        """Record a failure for ``agent_id``.

        First failure for an agent currently HEALTHY starts a one-shot
        timer that fires the HEALTHY -> STUCK transition once
        ``stuck_threshold_seconds`` elapse without a success. Subsequent
        failures while still in the window are no-ops.
        """
        aid_str = str(agent_id)
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            if record.health != AgentHealth.HEALTHY:
                return
            if record.first_failure_at is not None:
                return
            record.first_failure_at = time.monotonic()
            timer = threading.Timer(self.stuck_threshold_seconds, self._on_stuck_timer_fired, args=(aid_str,))
            timer.daemon = True
            self._cancel_stuck_timer_locked(aid_str)
            self._stuck_timers[aid_str] = timer
        timer.start()

    def record_success(self, agent_id: AgentId) -> None:
        """Record a successful probe for ``agent_id``.

        Clears any pending stuck timer; if the agent was STUCK or
        RESTARTING, transitions it back to HEALTHY and fires on-change.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.get(aid_str)
            self._cancel_stuck_timer_locked(aid_str)
            if record is None:
                return
            record.first_failure_at = None
            if record.health != AgentHealth.HEALTHY:
                record.health = AgentHealth.HEALTHY
                fire_health = AgentHealth.HEALTHY
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)

    def mark_stuck(self, agent_id: AgentId) -> None:
        """Force-transition ``agent_id`` to STUCK.

        Used by the restart endpoint to roll back a RESTARTING transition
        when the dispatch fails: without this, a failed dispatch would
        leave the recovery page permanently labelled "Restarting..." until
        an unrelated success/failure rewrote the state.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            self._cancel_stuck_timer_locked(aid_str)
            if record.health != AgentHealth.STUCK:
                record.health = AgentHealth.STUCK
                fire_health = AgentHealth.STUCK
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)

    def mark_restarting(self, agent_id: AgentId) -> None:
        """Mark ``agent_id`` as RESTARTING (called from the restart endpoint).

        Cancels any pending stuck timer (the agent is already known-bad and
        we don't need a delayed STUCK transition) and fires on-change so
        the recovery page can re-label.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            self._cancel_stuck_timer_locked(aid_str)
            record.first_failure_at = None
            if record.health != AgentHealth.RESTARTING:
                record.health = AgentHealth.RESTARTING
                fire_health = AgentHealth.RESTARTING
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)

    def get_health(self, agent_id: AgentId) -> AgentHealth:
        """Return the current health for ``agent_id`` (HEALTHY by default)."""
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return AgentHealth.HEALTHY
            return record.health

    def snapshot_all(self) -> dict[AgentId, AgentHealth]:
        """Return a copy of all currently-tracked non-HEALTHY agents.

        HEALTHY agents are omitted because the chrome auto-redirect and
        recovery page only care about agents with active recovery state;
        including every HEALTHY agent would make the SSE payload grow
        unboundedly with workspace count.
        """
        with self._lock:
            return {
                AgentId(aid): record.health
                for aid, record in self._records.items()
                if record.health != AgentHealth.HEALTHY
            }

    # -- Internals --------------------------------------------------------

    def _cancel_stuck_timer_locked(self, aid_str: str) -> None:
        timer = self._stuck_timers.pop(aid_str, None)
        if timer is not None:
            timer.cancel()

    def _on_stuck_timer_fired(self, aid_str: str) -> None:
        fire_health: AgentHealth | None = None
        with self._lock:
            self._stuck_timers.pop(aid_str, None)
            record = self._records.get(aid_str)
            if record is None:
                return
            if record.health != AgentHealth.HEALTHY:
                return
            if record.first_failure_at is None:
                return
            elapsed = time.monotonic() - record.first_failure_at
            if elapsed + 1e-6 < self.stuck_threshold_seconds:
                return
            record.health = AgentHealth.STUCK
            fire_health = AgentHealth.STUCK
        if fire_health is not None:
            self._fire_on_change(AgentId(aid_str), fire_health)

    def _fire_on_change(self, agent_id: AgentId, new_health: AgentHealth) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id, new_health)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("WorkspaceServerHealthTracker on-change callback failed for {}: {}", agent_id, e)
