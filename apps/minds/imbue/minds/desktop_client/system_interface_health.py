"""Tracks per-agent system-interface health for restart-recovery UX.

The plugin (``mngr_forward``) emits a ``system_interface_backend_failure``
envelope each time it observes a backend failure (connection failure, mid-SSE
EOF, or any non-2xx response). The plugin does not decide which of those
matter -- that policy lives here: ``should_enroll_suspect_for_backend_failure``
selects the ones that suggest the backend is unreachable, and minds routes only
those into ``record_failure``.

A failure envelope is only a *hint*. A single transient blip -- most commonly a
mid-SSE EOF when an SSE stream is recycled -- is not evidence that the workspace
is stuck, so ``record_failure`` never changes health on its own. It merely
enrolls the agent as a *suspect*: an agent the background probe loop should
start actively polling.

The background probe loop is the single authority on whether a workspace is
reachable. Each iteration it probes every suspect / non-HEALTHY agent and
reports the result back through ``record_probe_success`` / ``record_probe_failure``.
The state machine:

- HEALTHY -> STUCK: the probe loop observes an unbroken run of probe failures
  lasting at least ``stuck_threshold_seconds``. Every second of that run is
  backed by a real HTTP probe against the live workspace, so STUCK is never
  shown for an ephemeral signal. The chrome titlebar reacts by navigating the
  content view to the recovery page.
- STUCK -> RESTARTING: the restart endpoint marks the tracker so the recovery
  page can render a different label and the probe loop keeps polling.
- RESTARTING -> RESTART_FAILED: a restart tier failed to recover the workspace
  within its window, or its ``mngr`` commands errored. The recovery page
  renders the failure reason and an escalate / try-again affordance.
- {STUCK, RESTARTING, RESTART_FAILED} -> HEALTHY: a successful probe.

State changes fire registered on-change callbacks. Callbacks are invoked
outside the internal lock so they may take the FastAPI app's own locks
without deadlocking.

There is no timer: the only path to STUCK is sustained, probe-confirmed
failure. An agent that emits one bad request and then idles is still handled,
because the probe loop actively polls every suspect agent regardless of
whether further traffic arrives.
"""

import threading
import time
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
from imbue.mngr.primitives import AgentId

_DEFAULT_STUCK_THRESHOLD_SECONDS: Final[float] = 5.0

# HTTP statuses that suggest the backend itself is unreachable / not serving,
# as opposed to an application-layer error. The plugin reports every non-2xx
# response, but only these (or a connection-level failure carrying no status)
# enroll an agent as a probe suspect.
_BACKEND_UNREACHABLE_STATUSES: Final[frozenset[int]] = frozenset({502, 503, 504})


def should_enroll_suspect_for_backend_failure(status_code: int | None) -> bool:
    """Whether a ``system_interface_backend_failure`` should enroll a probe suspect.

    The plugin emits a failure envelope for every non-2xx response and for
    connection-level failures (which carry no status code). Minds acts only on
    the ones that suggest the backend is unreachable: a connection-level failure
    (``status_code is None``) or an infrastructure 5xx (502/503/504). Application
    errors (app 500s, ordinary 4xx) mean the backend is alive and responding, so
    they are left alone; the background probe still catches a genuinely-wrong or
    wedged backend.
    """
    return status_code is None or status_code in _BACKEND_UNREACHABLE_STATUSES


class AgentHealth(str, Enum):
    """Per-agent health classification used by the tracker + chrome SSE."""

    HEALTHY = "healthy"
    STUCK = "stuck"
    RESTARTING = "restarting"
    RESTART_FAILED = "restart_failed"


OnChangeCallback = Callable[[AgentId, AgentHealth], None]
OnRecoveryCallback = Callable[[AgentId], None]


class _AgentRecord(MutableModel):
    """Per-agent mutable state owned by the tracker. Not exposed to callers."""

    health: AgentHealth = Field(default=AgentHealth.HEALTHY)
    is_suspect: bool = Field(
        default=False,
        description=(
            "True once a failure envelope has enrolled this agent for active probing and "
            "no probe has since confirmed it reachable. Suspect HEALTHY agents are probe "
            "targets so the loop can decide STUCK; a successful probe clears the flag."
        ),
    )
    failure_run_started_at: float | None = Field(
        default=None,
        description=(
            "time.monotonic() of the first probe failure in the current unbroken run of "
            "probe failures, or None if the last probe succeeded or no probe has run yet. "
            "The HEALTHY -> STUCK transition fires once this run reaches stuck_threshold_seconds."
        ),
    )
    failure_run_started_wall_at: datetime | None = Field(
        default=None,
        description=(
            "Wall-clock (UTC) companion to ``failure_run_started_at``, captured at the same "
            "moment. ``failure_run_started_at`` is monotonic (correct for the stuck-threshold "
            "duration math but not comparable to wall-clock timestamps); this field exists so "
            "the recovery redirect can compare the outage onset against discovery snapshot "
            "timestamps. None whenever ``failure_run_started_at`` is None."
        ),
    )
    last_restart_error: str | None = Field(
        default=None,
        description="Failure reason carried while health is RESTART_FAILED, for the recovery page to render.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class SystemInterfaceHealthTracker(MutableModel):
    """Per-agent health state machine driven by failure envelopes + probe results.

    Construct one per minds process; share with the envelope-consumer callback
    (which calls ``record_failure``), the background probe loop (which calls
    ``record_probe_success`` / ``record_probe_failure``), the restart worker
    (``mark_restarting`` / ``mark_restart_failed`` / ``record_probe_success``),
    and the chrome SSE generator (which subscribes via ``add_on_change_callback``).
    """

    stuck_threshold_seconds: float = Field(
        default=_DEFAULT_STUCK_THRESHOLD_SECONDS,
        description="Seconds of continuous probe failures before HEALTHY -> STUCK fires.",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _records: dict[str, _AgentRecord] = PrivateAttr(default_factory=dict)
    _on_change_callbacks: list[OnChangeCallback] = PrivateAttr(default_factory=list)
    _on_recovery_callbacks: list[OnRecoveryCallback] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # -- Public callback registration -------------------------------------

    def add_on_change_callback(self, callback: OnChangeCallback) -> None:
        """Register a callback fired whenever any agent's health changes.

        Callbacks receive ``(agent_id, new_health)`` and run on whichever
        thread caused the transition (probe loop or restart worker).
        Callbacks must be fast and non-blocking; do real work on a queue or
        worker thread.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    def add_on_recovery_callback(self, callback: OnRecoveryCallback) -> None:
        """Register a callback fired on every non-HEALTHY -> HEALTHY transition.

        Distinct from ``add_on_change_callback`` so consumers that only care
        about successful recoveries don't have to filter the firehose of
        every state change. The recovery-diagnostics path uses this to
        write the final probe results at INFO via loguru.
        """
        with self._lock:
            self._on_recovery_callbacks.append(callback)

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
        """Enroll ``agent_id`` as a suspect probe target. Does NOT change health.

        Called for each ``system_interface_backend_failure`` envelope. A
        failure envelope is only a hint that the workspace *might* be unhealthy
        -- it could just be a recycled SSE stream -- so this method never
        transitions health by itself. It only flags the agent so the
        background probe loop starts actively polling it; the probe loop's
        observations decide STUCK. Idempotent.
        """
        aid_str = str(agent_id)
        with self._lock:
            record = self._records.get(aid_str)
            if record is None:
                record = _AgentRecord()
                self._records[aid_str] = record
            record.is_suspect = True

    def record_probe_failure(self, agent_id: AgentId) -> None:
        """Record that a background probe observed ``agent_id`` as unreachable.

        Starts the agent's probe-failure run on the first failure, then
        transitions HEALTHY -> STUCK once the run has lasted at least
        ``stuck_threshold_seconds``. Probe failures for an agent that is not
        HEALTHY (already STUCK, or RESTARTING / RESTART_FAILED -- states owned
        by the restart flow) or that has no record (de-enrolled concurrently)
        are ignored.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.get(aid_str)
            if record is None or record.health != AgentHealth.HEALTHY:
                return
            now = time.monotonic()
            if record.failure_run_started_at is None:
                record.failure_run_started_at = now
                record.failure_run_started_wall_at = datetime.now(timezone.utc)
            elapsed = now - record.failure_run_started_at
            if elapsed + 1e-6 < self.stuck_threshold_seconds:
                return
            record.health = AgentHealth.STUCK
            fire_health = AgentHealth.STUCK
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)

    def record_probe_success(self, agent_id: AgentId) -> None:
        """Record that a probe observed ``agent_id`` responding (HTTP 200).

        Clears the agent's probe-failure run and suspect flag. If the agent was
        STUCK, RESTARTING, or RESTART_FAILED, transitions it back to HEALTHY and
        fires on-change. The now-clean record is dropped so ``_records`` stays
        scoped to agents that still need attention.

        Called by the background probe loop on a 200, and by the restart worker
        and the creation-time readiness wait, whose own probes are equally
        authoritative.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.pop(aid_str, None)
            if record is None:
                return
            if record.health != AgentHealth.HEALTHY:
                fire_health = AgentHealth.HEALTHY
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)
            self._fire_on_recovery(agent_id)

    def mark_stuck(self, agent_id: AgentId) -> None:
        """Force-transition ``agent_id`` to STUCK, firing on-change.

        Unconditionally sets the agent's health to STUCK, regardless of any
        in-progress probe-failure run. Idempotent: a call on an
        already-STUCK agent is a no-op and does not re-fire on-change.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            if record.health != AgentHealth.STUCK:
                record.health = AgentHealth.STUCK
                fire_health = AgentHealth.STUCK
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)

    def mark_restarting(self, agent_id: AgentId) -> bool:
        """Mark ``agent_id`` as RESTARTING (called from the restart endpoint).

        Clears any in-progress probe-failure run (the agent is already
        known-bad) and fires on-change so the recovery page can re-label.

        Returns ``True`` if this call transitioned the agent into RESTARTING
        (the agent was not already RESTARTING), and ``False`` if it was already
        RESTARTING. The transition is decided under the internal lock, so
        callers can use the return value as an atomic compare-and-set to ensure
        exactly one of several concurrent restart requests proceeds.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            record.failure_run_started_at = None
            record.failure_run_started_wall_at = None
            # A fresh restart attempt supersedes any prior failure reason.
            record.last_restart_error = None
            if record.health != AgentHealth.RESTARTING:
                record.health = AgentHealth.RESTARTING
                fire_health = AgentHealth.RESTARTING
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)
        return fire_health is not None

    def mark_restart_failed(self, agent_id: AgentId, error: str) -> None:
        """Mark ``agent_id`` as RESTART_FAILED, carrying ``error`` as the reason.

        Called when a restart tier fails to recover the workspace within its
        window, or its ``mngr`` commands error out. The reason is surfaced to
        the recovery page so it can render an escalate / try-again affordance
        instead of an indefinite "Restarting...".
        """
        aid_str = str(agent_id)
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            record.failure_run_started_at = None
            record.failure_run_started_wall_at = None
            record.last_restart_error = error
            # Always re-fire: a second failure with a new reason must reach
            # the recovery page even if the state is already RESTART_FAILED.
            record.health = AgentHealth.RESTART_FAILED
        self._fire_on_change(agent_id, AgentHealth.RESTART_FAILED)

    def get_health(self, agent_id: AgentId) -> AgentHealth:
        """Return the current health for ``agent_id`` (HEALTHY by default)."""
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return AgentHealth.HEALTHY
            return record.health

    def get_last_restart_error(self, agent_id: AgentId) -> str | None:
        """Return the failure reason for ``agent_id`` if it is RESTART_FAILED."""
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return None
            return record.last_restart_error

    def get_failure_run_started_wall_at(self, agent_id: AgentId) -> datetime | None:
        """Return the wall-clock (UTC) start of the current probe-failure run, or None.

        Approximates the outage onset -- the run begins on the first failed probe,
        which is when the workspace stopped answering. The recovery redirect uses
        this to require a discovery snapshot taken *after* the outage began before
        it trusts the snapshot's host state. None when no probe-failure run is
        active (the agent is healthy, or was force-marked STUCK without a run).
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return None
            return record.failure_run_started_wall_at

    def snapshot_all(self) -> dict[AgentId, AgentHealth]:
        """Return a copy of all currently-tracked non-HEALTHY agents.

        HEALTHY agents (including suspect ones) are omitted because the chrome
        auto-redirect and recovery page only care about agents with active
        recovery state; a suspect-but-still-HEALTHY agent must not redirect the
        chrome to the recovery page.
        """
        with self._lock:
            return {
                AgentId(aid): record.health
                for aid, record in self._records.items()
                if record.health != AgentHealth.HEALTHY
            }

    def snapshot_probe_targets(self) -> frozenset[AgentId]:
        """Return every agent the background probe loop should poll this tick.

        An agent is a probe target when it is suspect (a failure envelope
        enrolled it and no probe has since cleared it), STUCK, or
        RESTART_FAILED -- the loop polls those for recovery. HEALTHY
        non-suspect agents are omitted; probing every workspace unconditionally
        would scale probe traffic with workspace count for no benefit.

        RESTARTING agents are deliberately excluded: while the restart worker
        is in flight, the *old* system interface is still answering 200 in the
        window between ``mark_restarting`` and the worker's ``mngr stop``
        actually tearing down the backend. A background probe in that window
        would prematurely flip the agent back to HEALTHY (via
        ``record_probe_success``), causing the recovery page to 302 the user
        back into a workspace that is about to disappear. The restart worker
        owns the recovery decision via its own ``_await_system_interface_ready``
        probe, which only runs *after* the stop step completes.
        """
        with self._lock:
            return frozenset(
                AgentId(aid)
                for aid, record in self._records.items()
                if (record.is_suspect and record.health == AgentHealth.HEALTHY)
                or record.health in (AgentHealth.STUCK, AgentHealth.RESTART_FAILED)
            )

    # -- Internals --------------------------------------------------------

    def _fire_on_change(self, agent_id: AgentId, new_health: AgentHealth) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id, new_health)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("SystemInterfaceHealthTracker on-change callback failed for {}: {}", agent_id, e)

    def _fire_on_recovery(self, agent_id: AgentId) -> None:
        with self._lock:
            callbacks = list(self._on_recovery_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("SystemInterfaceHealthTracker on-recovery callback failed for {}: {}", agent_id, e)
