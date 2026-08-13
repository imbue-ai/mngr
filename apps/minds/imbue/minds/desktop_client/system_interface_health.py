"""Tracks per-agent system-interface health for restart-recovery UX.

The plugin (``mngr_forward``) emits a ``system_interface_backend_failure``
envelope each time it observes a backend failure (connection failure, mid-SSE
EOF, or any non-2xx response), and one more -- ``STALLED`` -- for a request the
backend has not answered yet, which may still succeed. The plugin does not
decide which of those matter -- that policy lives here:
``should_enroll_suspect_for_backend_failure`` selects the ones that suggest the
backend is unreachable, and minds routes only those into ``record_failure``.

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
  shown for an ephemeral signal. The SPA shows a notice band over the
  still-rendered machine, and unattended recovery dispatches a start-only
  restart without waiting to be asked.
- STUCK -> RESTARTING: the restart dispatch marks the tracker so the recovery
  card can render a different label. The background loop stands off a
  RESTARTING agent (see ``snapshot_probe_targets``); the restart worker's own
  readiness probe is what decides whether the machine came back.
- RESTARTING -> RESTART_FAILED: a restart failed to recover the workspace
  within its window, or its ``mngr`` commands errored. The recovery card
  renders the failure reason and the restart affordance.
- {STUCK, RESTARTING, RESTART_FAILED} -> HEALTHY: a successful probe.

State changes fire registered on-change callbacks. Callbacks are invoked
outside the internal lock so they may take the FastAPI app's own locks
without deadlocking.

The probe-confirmed HEALTHY -> STUCK edge additionally fires the *stuck-edge*
callbacks, a separate channel for consumers that dispatch work. Only this edge
means "an outage just started, nothing has been tried yet": it is the sole path
into STUCK that a probe run established, and it carries the failure-run onset
the recovery verdict reads. ``mark_stuck`` forces the same state with neither.

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
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason

_DEFAULT_STUCK_THRESHOLD_SECONDS: Final[float] = 5.0

# HTTP statuses that suggest the backend itself is unreachable / not serving,
# as opposed to an application-layer error. The plugin reports every non-2xx
# response, but only these (or a connection-level failure carrying no status)
# enroll an agent as a probe suspect.
_BACKEND_UNREACHABLE_STATUSES: Final[frozenset[int]] = frozenset({502, 503, 504})


def should_enroll_suspect_for_backend_failure(
    reason: SystemInterfaceBackendFailureReason,
    status_code: int | None,
) -> bool:
    """Whether a ``system_interface_backend_failure`` should enroll a probe suspect.

    The plugin emits a failure envelope for every non-2xx response, for
    connection-level failures (which carry no status code), and -- as
    ``STALLED`` -- for a request still in flight that the backend has not
    answered within the plugin's stall window. Minds acts only on the ones that
    suggest the backend is unreachable: anything without a status code
    (``CONNECT_ERROR`` / ``SSE_EOF`` / ``STALLED``) or an infrastructure 5xx
    (502/503/504). Application errors (app 500s, ordinary 4xx) mean the backend
    is alive and responding, so they are left alone; the background probe still
    catches a genuinely-wrong or wedged backend.

    ``STALLED`` is deliberately treated the same as a hard failure even though
    the request may still succeed: a wedged backend is indistinguishable from a
    slow one at that moment, and guessing wrong is cheap in only one direction.
    Enrolling a merely-slow backend costs one probe, which answers 200 and
    clears the flag; declining to enroll a wedged one leaves it undetected,
    because a HEALTHY non-suspect agent is never probed.

    ``UNRESOLVED`` is ignored outright: it means the forward has no route for the
    agent at all. A restart routes *through* the forward, so it cannot help a
    routeless agent regardless. In practice ``UNRESOLVED`` is either a cold-start
    / fresh-forward warm-up (the forward has not caught up to discovery yet --
    this self-resolves the moment it does, so enrolling would only mark a healthy
    workspace STUCK and needlessly restart it) or a genuinely-gone agent (which a
    restart cannot revive). A workspace that is present but unreachable does NOT
    land here: discovery retains its (stale) route, so the dial failure surfaces
    as ``CONNECT_ERROR`` / a 5xx, which still enrolls and still drives recovery.
    """
    if reason == SystemInterfaceBackendFailureReason.UNRESOLVED:
        return False
    return status_code is None or status_code in _BACKEND_UNREACHABLE_STATUSES


class AgentHealth(str, Enum):
    """Per-agent health classification used by the tracker + the /ui/ws channel."""

    HEALTHY = "healthy"
    STUCK = "stuck"
    RESTARTING = "restarting"
    RESTART_FAILED = "restart_failed"


OnChangeCallback = Callable[[AgentId, AgentHealth], None]
OnRecoveryCallback = Callable[[AgentId], None]
OnStuckEdgeCallback = Callable[[AgentId], None]


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
    restart_is_start_only: bool | None = Field(
        default=None,
        description=(
            "Whether the in-flight RESTARTING restart is start-only (an idempotent ``mngr start`` "
            "that may be a no-op) rather than a full stop+start bounce. Set when a restart wins the "
            "RESTARTING transition and read only while RESTARTING -- the recovery page picks its "
            "restarting-state copy from it (a full manual bounce reads as 'Restarting your machine', "
            "a start-only entry dispatch as the neutral 'Loading machine'). None outside a restart."
        ),
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class SystemInterfaceHealthTracker(MutableModel):
    """Per-agent health state machine driven by failure envelopes + probe results.

    Construct one per minds process; share with the envelope-consumer callback
    (which calls ``record_failure``), the background probe loop (which calls
    ``record_probe_success`` / ``record_probe_failure``), the restart worker
    (``mark_restarting`` / ``mark_restart_failed`` / ``record_probe_success``),
    and the callback subscribers wired in ``app.py``.
    """

    stuck_threshold_seconds: float = Field(
        default=_DEFAULT_STUCK_THRESHOLD_SECONDS,
        description="Seconds of continuous probe failures before HEALTHY -> STUCK fires.",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _records: dict[str, _AgentRecord] = PrivateAttr(default_factory=dict)
    _on_change_callbacks: list[OnChangeCallback] = PrivateAttr(default_factory=list)
    _on_recovery_callbacks: list[OnRecoveryCallback] = PrivateAttr(default_factory=list)
    _on_stuck_edge_callbacks: list[OnStuckEdgeCallback] = PrivateAttr(default_factory=list)
    # agent_id_str -> time.monotonic() deadline of an initial-create-attempt grace window.
    # While a create attempt is in flight (and until its readiness window expires), probe
    # failures must not drive the agent to STUCK: a cold build-in-VM Lima create
    # legitimately serves 503s for many minutes, and bouncing the user to the
    # recovery page mid-provisioning is exactly the takeover this suppresses.
    _create_attempt_grace_deadline_by_agent: dict[str, float] = PrivateAttr(default_factory=dict)
    # Agents stopped from inside the app. Their interface is legitimately
    # unreachable, so the unattended dispatch must not read STUCK as a failure
    # and start the host back up (see suppress_unattended_recovery).
    _unattended_recovery_suppressed_agents: set[str] = PrivateAttr(default_factory=set)
    # The subset of those whose stop command has not returned yet. Their interface
    # is still answering, so a probe success is the stop in progress rather than
    # the machine back, and must not drop the mark above.
    _in_flight_intentional_stop_agents: set[str] = PrivateAttr(default_factory=set)

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

    def add_on_stuck_edge_callback(self, callback: OnStuckEdgeCallback) -> None:
        """Register a callback fired only on the probe-confirmed HEALTHY -> STUCK edge.

        Narrower than :meth:`add_on_change_callback` on purpose: only this edge
        means "an outage just started, nothing has been tried yet", and it fires
        exactly once per outage episode, so a consumer that dispatches work (the
        unattended restart) needs no latch of its own.

        Not the same as filtering on-change for a STUCK result. :meth:`mark_stuck`
        forces the state from anywhere, with no probe run behind it and no
        failure-run onset recorded, so a consumer keyed on the state would be
        dispatching against an assertion rather than against observed evidence.
        """
        with self._lock:
            self._on_stuck_edge_callbacks.append(callback)

    def remove_on_change_callback(self, callback: OnChangeCallback) -> None:
        """Unregister a previously registered change callback.

        Safe to call even if the callback is not currently registered (no-op).
        """
        with self._lock:
            try:
                self._on_change_callbacks.remove(callback)
            except ValueError:
                pass

    # -- CreateAttempt grace ----------------------------------------------------

    def begin_create_attempt_grace(self, agent_id: AgentId, deadline_monotonic: float) -> None:
        """Suppress probe-failure STUCK transitions for ``agent_id`` until ``deadline_monotonic``.

        Called by the create attempt flow right before its readiness wait, once the
        canonical agent id is known. While the grace is active, probe failures
        are ignored outright (no failure run accumulates), so a workspace still
        provisioning is never driven to STUCK. The grace ends at the deadline
        (the create attempt's readiness window expiring), on :meth:`end_create_attempt_grace`
        (the create attempt reaching a terminal status), or on a successful probe --
        whichever comes first. Applies to initial create attempt only; the start /
        restart paths never register a grace.
        """
        with self._lock:
            self._create_attempt_grace_deadline_by_agent[str(agent_id)] = deadline_monotonic
        logger.debug("Began create attempt grace for {} (until monotonic {:.0f})", agent_id, deadline_monotonic)

    def end_create_attempt_grace(self, agent_id: AgentId) -> None:
        """Drop any create attempt grace for ``agent_id``. Idempotent."""
        with self._lock:
            existed = self._create_attempt_grace_deadline_by_agent.pop(str(agent_id), None) is not None
        if existed:
            logger.debug("Ended create attempt grace for {}", agent_id)

    def _is_create_attempt_grace_active_locked(self, aid_str: str) -> bool:
        """Whether an unexpired create attempt grace exists for the agent. Must hold ``self._lock``.

        An expired grace is dropped on observation so the map stays bounded even
        if the create attempt thread died before calling :meth:`end_create_attempt_grace`.
        """
        deadline = self._create_attempt_grace_deadline_by_agent.get(aid_str)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            del self._create_attempt_grace_deadline_by_agent[aid_str]
            return False
        return True

    # -- Intentional stops ------------------------------------------------

    def suppress_unattended_recovery(self, agent_id: AgentId, *, is_stop_in_flight: bool = False) -> None:
        """Mark ``agent_id`` as deliberately stopped, so nothing auto-starts it.

        A stopped host's system interface is unreachable, which is
        indistinguishable from a wedge by probing alone: the agent goes STUCK
        either way. Without this marker the unattended dispatch would read a
        stop the user just asked for as a failure and start the host again,
        silently undoing it -- and on a metered provider, billing them for it.

        Remembered intent rather than observed state: a host that crashed or
        shut down on its own reads STOPPED too, and that is exactly the case
        unattended recovery exists for.

        Set by the in-app stop -- before its ``mngr stop`` runs, and again once
        that stop has succeeded -- by the quit prompt's bulk stop
        (:func:`stop_workspace_hosts`), which needs it because a partial quit
        offers Cancel quit and hands back an app whose machines are down, and by
        the destroy route, whose machine is on its way to not existing at all.
        Cleared by the in-app start, or by any probe that finds the machine
        answering again. That probe clear is what makes the mark self-limiting:
        a stopped machine can also be started by a route that never touches the
        start endpoint (the machines-list click-through dispatches a start-only
        restart), and those machines would otherwise stay suppressed for the
        life of the process.

        ``is_stop_in_flight`` covers the one window in which that probe clear
        would be wrong: a stop's own command, which blocks for tens of seconds
        (a cloud host, minutes) while the interface goes on answering for the
        first of them. A machine being stopped is often a probe target already
        (suspect-enrolled, STUCK, or RESTART_FAILED), so that 200 gets taken,
        and dropping the mark on it would hand the machine to the dispatch
        mid-stop. While a stop is in flight a probe success leaves the mark
        alone; the stop closes the window when its command returns, either by
        marking again with the default (keeping the mark, dropping the flag) or
        by calling :meth:`allow_unattended_recovery` if it failed. A destroy
        marks in flight and never reconciles: it is answered while the teardown
        is still running, and a machine that is gone has no 200 left to give.

        Scoped to this process, and to teardowns that went through those paths.
        A machine stopped from the CLI, or left stopped across an app restart,
        carries no mark, so merely reaching it is enough to auto-start it --
        session restore does exactly that, and that is the point: a user who
        accepted the quit prompt expects to be put back where they were.
        """
        aid_str = str(agent_id)
        with self._lock:
            self._unattended_recovery_suppressed_agents.add(aid_str)
            if is_stop_in_flight:
                self._in_flight_intentional_stop_agents.add(aid_str)
            else:
                self._in_flight_intentional_stop_agents.discard(aid_str)
        logger.debug("Suppressed unattended recovery for {} (stopped from inside the app)", agent_id)

    def allow_unattended_recovery(self, agent_id: AgentId) -> None:
        """Drop any intentional-stop marker for ``agent_id``, in flight or not. Idempotent."""
        aid_str = str(agent_id)
        with self._lock:
            existed = aid_str in self._unattended_recovery_suppressed_agents
            self._unattended_recovery_suppressed_agents.discard(aid_str)
            self._in_flight_intentional_stop_agents.discard(aid_str)
        if existed:
            logger.debug("Allowed unattended recovery for {} again", agent_id)

    def is_unattended_recovery_suppressed(self, agent_id: AgentId) -> bool:
        """Whether ``agent_id`` was stopped from inside the app and left stopped."""
        with self._lock:
            return str(agent_id) in self._unattended_recovery_suppressed_agents

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
            was_suspect = record.is_suspect
            record.is_suspect = True
        # Only the False -> True edge is interesting; duplicate envelopes for an
        # already-suspect agent are noise. Enrollment is the entry point of the
        # recovery lifecycle, so it is the one log worth keeping at this layer.
        if not was_suspect:
            logger.debug("Enrolled {} as a system-interface probe suspect (backend-failure envelope)", agent_id)

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
        stuck_after_seconds: float | None = None
        with self._lock:
            # An in-flight create attempt's readiness window suppresses failure
            # accounting entirely: 503s while the workspace is still
            # provisioning are expected, not evidence of a wedge. After the
            # grace expires, the normal stuck-threshold run applies from
            # scratch, so a genuinely wedged workspace still reaches STUCK.
            if self._is_create_attempt_grace_active_locked(aid_str):
                return
            record = self._records.get(aid_str)
            if record is None or record.health != AgentHealth.HEALTHY:
                return
            now = time.monotonic()
            if record.failure_run_started_at is None:
                record.failure_run_started_at = now
                record.failure_run_started_wall_at = datetime.now(timezone.utc)
            elapsed = now - record.failure_run_started_at
            if elapsed + 1e-6 >= self.stuck_threshold_seconds:
                record.health = AgentHealth.STUCK
                fire_health = AgentHealth.STUCK
                stuck_after_seconds = elapsed
        # The STUCK edge is the key diagnostic; the elapsed time tells us exactly
        # how long the workspace was continuously failing before it tripped.
        if fire_health is not None and stuck_after_seconds is not None:
            logger.info(
                "System-interface health for {}: HEALTHY -> STUCK after {:.1f}s of continuous probe failures",
                agent_id,
                stuck_after_seconds,
            )
            self._fire_on_change(agent_id, fire_health)
            self._fire_on_stuck_edge(agent_id)

    def record_probe_success(self, agent_id: AgentId) -> None:
        """Record that a probe observed ``agent_id`` responding (HTTP 200).

        Clears the agent's probe-failure run and suspect flag. If the agent was
        STUCK, RESTARTING, or RESTART_FAILED, transitions it back to HEALTHY and
        fires on-change. The now-clean record is dropped so ``_records`` stays
        scoped to agents that still need attention.

        Called by the background probe loop on a 200, and by the restart worker
        and the create-attempt-time readiness wait, whose own probes are equally
        authoritative.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        prior_health: AgentHealth | None = None
        with self._lock:
            # A reachable workspace no longer needs its create attempt grace.
            self._create_attempt_grace_deadline_by_agent.pop(aid_str, None)
            # Nor is it still the stopped machine the marker was set for, no
            # matter which route started it back up. A machine whose stop
            # command has not returned yet is the exception: its interface
            # answers for the first seconds of the stop, so this 200 is the stop
            # in progress rather than the machine back.
            if aid_str not in self._in_flight_intentional_stop_agents:
                self._unattended_recovery_suppressed_agents.discard(aid_str)
            record = self._records.pop(aid_str, None)
            if record is None:
                return
            if record.health != AgentHealth.HEALTHY:
                prior_health = record.health
                fire_health = AgentHealth.HEALTHY
        if fire_health is not None:
            logger.info(
                "System-interface health for {}: {} -> HEALTHY (probe succeeded)",
                agent_id,
                prior_health.value if prior_health is not None else "?",
            )
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

    def mark_restarting(self, agent_id: AgentId, start_only: bool) -> bool:
        """Mark ``agent_id`` as RESTARTING (called from the restart endpoint).

        Clears any in-progress probe-failure run (the agent is already
        known-bad) and fires on-change so the recovery page can re-label.
        ``start_only`` records the restart's flavor (an idempotent ``mngr start``
        vs a full stop+start bounce) so the recovery page can name the wait; it
        is recorded only when this call wins the transition, so a deduped later
        request never rewrites the flavor of the restart already in flight.

        Returns whether this call transitioned the agent into RESTARTING (it
        was not already RESTARTING). That reports the transition; it does not
        decide who owns the restart. Ownership is the workspace's single
        operation slot, which ``dispatch_host_restart`` wins before it gets
        here -- a tracker-side compare-and-set could not serialize against the
        backup operations, which never touch the tracker.
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
                record.restart_is_start_only = start_only
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

    def get_restart_is_start_only(self, agent_id: AgentId) -> bool | None:
        """Return whether the in-flight restart is start-only, or None if not RESTARTING.

        Only meaningful while the agent is RESTARTING; returns None otherwise so a
        stale value from a prior restart is never read. The recovery page uses it
        to pick the restarting-state copy -- a full manual bounce reads as
        "Restarting your machine", a start-only entry dispatch (which may be a
        no-op) as the neutral "Loading machine".
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None or record.health != AgentHealth.RESTARTING:
                return None
            return record.restart_is_start_only

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

    def _fire_on_stuck_edge(self, agent_id: AgentId) -> None:
        with self._lock:
            callbacks = list(self._on_stuck_edge_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("SystemInterfaceHealthTracker stuck-edge callback failed for {}: {}", agent_id, e)
