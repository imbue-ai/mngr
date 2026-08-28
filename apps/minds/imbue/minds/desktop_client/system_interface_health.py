"""Tracks per-agent system-interface health for the machine-recovery UX.

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
  still-rendered machine, and unattended recovery *starts* the machine without
  waiting to be asked (an idempotent ``mngr start``, never a bounce).
- STUCK -> RECOVERING: the recovery dispatch marks the tracker so the recovery
  card can render a different label. The background loop stands off a
  RECOVERING agent (see ``snapshot_probe_targets``); the recovery worker's own
  readiness probe is what decides whether the machine came back.
- RECOVERING -> RECOVERY_FAILED: a recovery failed to bring the workspace back
  within its window, or its ``mngr`` commands errored. The recovery card
  renders the failure reason and the restart affordance.
- {STUCK, RECOVERING, RECOVERY_FAILED} -> HEALTHY: a successful probe.

Which of the two recoveries ran is :class:`HostRecoveryKind`, and the surfaces
turn on it: only the user's own click stops the machine, so only that one may be
narrated as a restart.

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

That run must also be *observed* failure, not merely elapsed failure. A laptop
that sleeps mid-run stops the probe loop along with everything else, so the
seconds it slept were backed by no probe at all; a run that straddles a sleep
is restarted from the first failure observed after it (see ``sleep_tracker``),
and the threshold is reached only once it has accumulated entirely while the
process was running.
"""

import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from enum import Enum
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.environment_signals import SleepTracker
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
    (``CONNECT_ERROR`` / ``TUNNEL_SETUP_FAILED`` / ``POOL_EXHAUSTED`` /
    ``BACKEND_NOT_LISTENING`` / ``SSE_EOF`` / ``STALLED``) or an infrastructure
    5xx (502/503/504). Application errors (app 500s, ordinary 4xx) mean the
    backend is alive and responding, so they are left alone; the background probe
    still catches a genuinely-wrong or wedged backend.

    The three causes split out of ``CONNECT_ERROR`` enroll exactly as it does,
    deliberately. What they change is what the surfaces *claim* while the probe
    runs, not whether the probe runs: even a failure that is provably this
    device's fault leaves the workspace's own reachability unestablished, and a
    probe is what establishes it. Declining to enroll on them would trade a
    wrong label for a blind spot.

    ``STALLED`` is deliberately treated the same as a hard failure even though
    the request may still succeed: a wedged backend is indistinguishable from a
    slow one at that moment, and guessing wrong is cheap in only one direction.
    Enrolling a merely-slow backend costs one probe, which answers 200 and
    clears the flag; declining to enroll a wedged one leaves it undetected,
    because a HEALTHY non-suspect agent is never probed.

    ``UNRESOLVED`` is ignored outright: it means the forward has no route for the
    agent at all. A recovery routes *through* the forward, so it cannot help a
    routeless agent regardless. In practice ``UNRESOLVED`` is either a cold-start
    / fresh-forward warm-up (the forward has not caught up to discovery yet --
    this self-resolves the moment it does, so enrolling would only mark a healthy
    workspace STUCK and needlessly start it) or a genuinely-gone agent (which a
    start cannot revive). A workspace that is present but unreachable does NOT
    land here: discovery retains its (stale) route, so the dial failure surfaces
    as ``CONNECT_ERROR`` / a 5xx, which still enrolls and still drives recovery.
    """
    if reason == SystemInterfaceBackendFailureReason.UNRESOLVED:
        return False
    return status_code is None or status_code in _BACKEND_UNREACHABLE_STATUSES


# The reasons that report a connection which never carried a response: the
# backend was not reached at all. Each names a different cause, and which one it
# was decides whether the workspace is implicated -- so these are the reasons
# whose cause is worth recording. ``SSE_EOF`` is excluded because the connection
# demonstrably worked (the response had started), and ``STALLED`` because the
# request has not failed at all.
_CONNECTION_CLASS_REASONS: Final[frozenset[SystemInterfaceBackendFailureReason]] = frozenset(
    {
        SystemInterfaceBackendFailureReason.CONNECT_ERROR,
        SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED,
        SystemInterfaceBackendFailureReason.POOL_EXHAUSTED,
        SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING,
    }
)

# How long an established cause keeps outranking the residual ``CONNECT_ERROR``
# without being reported again (see ``record_connection_failure``). The forward
# re-emits a cause that is still happening at roughly 1 Hz, so this is more than
# an order of magnitude of headroom against flapping -- while a cause that has
# stopped happening (a pool that refilled, a socket bind that succeeded on the
# next try) stops speaking for the machine on the next envelope after the
# window, before a user reads a card that would otherwise blame their device for
# an outage that has since become the machine's.
#
# It is a rule about the next envelope, not a timer: nothing ages the recorded
# cause out on its own. So it does not fire across a stretch where nothing goes
# through the forward at all -- notably while a recovery worker is running its
# ``mngr start`` (preceded by an ``mngr stop`` when the user asked for a
# restart), since the probe loop excludes RECOVERING agents and the readiness
# probes only begin once the start returns. A cause recorded just before a
# recovery therefore keeps speaking for the length of those commands, and is
# displaced by the first readiness probe after them.
_DEFAULT_ESTABLISHED_CAUSE_DEFERENCE_SECONDS: Final[float] = 15.0

# How long one cause may go unlogged for an agent while it keeps being reported
# (see ``record_connection_failure``). Needed because the recorded observation is
# dropped with the rest of the episode on every probe success, and a failure that
# is not the system interface's own can repeat indefinitely across those
# successes: the forward emits one envelope per *agent*, so any registered
# service that stops listening -- a dev server the user shut down, say -- reports
# a connection failure at the forward's retry cadence while the machine itself
# answers every probe. Measured against a healthy workspace with one dead
# service, that is a line every two seconds for as long as the tab stays open.
# The interval keeps the cause on the record where the surfaces read it and only
# rations the log, so the breadcrumb still marks the incident without burying it.
_DEFAULT_CONNECTION_FAILURE_LOG_INTERVAL_SECONDS: Final[float] = 300.0


class AgentHealth(str, Enum):
    """Per-agent health classification used by the tracker + the /ui/ws channel.

    Neither recovery value says the machine was stopped, because the unattended
    path only starts it. Which of the two ran is ``HostRecoveryKind``; the state
    name deliberately does not answer that.

    These values reach no agent-facing surface -- they are absent from every
    ``/api/v1`` model and from the OpenAPI document -- so all three consumers
    ship from this repo: the SPA's ``WorkspaceHealth``, the
    ``/ui/api/.../recovery-info`` payload, and ``electron/main.js``, which
    compares against the bare string in plain JS no type checker covers. Any
    further change to them is a ``UI_SCHEMA_VERSION`` bump.
    """

    HEALTHY = "healthy"
    STUCK = "stuck"
    RECOVERING = "recovering"
    RECOVERY_FAILED = "recovery_failed"


class HostRecoveryKind(LowerCaseStrEnum):
    """Which of the two host-recovery actions an episode is running.

    The distinction is the stop step, and it is the whole reason the surfaces
    cannot describe the two the same way: only RESTART takes the machine down.
    """

    # ``mngr start`` alone: idempotent, no-ops against a host that is already
    # running, never bounces a live container. What unattended recovery
    # dispatches on the STUCK edge, and what the stopped-machine click-through
    # asks for.
    START = auto()
    # ``mngr stop --stop-host`` then ``mngr start``: a real bounce, and the only
    # recovery that takes the machine down. Reached solely from the user's own
    # "Restart machine" click, since a running-but-wedged container is the one
    # case a start cannot fix.
    RESTART = auto()


OnChangeCallback = Callable[[AgentId, AgentHealth], None]
OnRecoveryCallback = Callable[[AgentId], None]
OnStuckEdgeCallback = Callable[[AgentId], None]


class BackendOutageObservation(FrozenModel):
    """A backend outage observed in-band, by a command mngr rejected at the provider.

    Held by the tracker so the recovery surfaces can read it, because such a
    rejection is the *first* observation of an outage anywhere: it comes from a
    live command, whereas the discovery snapshot that would otherwise carry the
    same outage is up to a provider poll interval away. ``observed_at`` is what
    bounds its authority -- see ``get_backend_outage``.
    """

    provider_name: str = Field(description="Provider instance the command was rejected at")
    reason: str = Field(description="That provider's own account of why it is unavailable")
    observed_at: datetime = Field(description="Wall-clock (UTC) moment the rejection was observed")


class ConnectionFailureObservation(FrozenModel):
    """The classified cause of the connection-class failures this episode is made of.

    The forward tells three unreachable-backend causes apart that used to arrive
    as one, and two of them are not about the workspace at all: a tunnel this
    device could not build, and the forward's own pool running out. Holding the
    cause here is what lets the recovery surfaces stop blaming the machine for
    them, and what makes the split measurable in a bug report.
    """

    reason: SystemInterfaceBackendFailureReason = Field(description="What the forward classified the failure as")
    detail: str | None = Field(default=None, description="The forward's verbatim error text, when it quoted one")
    last_observed_at: datetime = Field(
        description=(
            "Wall-clock (UTC) moment the forward last reported this cause. Refreshed on every repeat, "
            "which is what tells a cause that is still happening from one that has stopped."
        )
    )


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
    outage_started_wall_at: datetime | None = Field(
        default=None,
        description=(
            "Wall-clock (UTC) start of this whole unhealthy episode, captured with the first "
            "probe failure that opened it. Unlike ``failure_run_started_wall_at`` it survives "
            "the recovery attempts made during the episode -- those clear the failure *run* "
            "because the machine is already known-bad, but the outage they are responding to "
            "began when the machine stopped answering, and evidence gathered before that "
            "moment describes the world before it. Cleared with the record when a probe "
            "observes the machine answering again."
        ),
    )
    last_recovery_error: str | None = Field(
        default=None,
        description="Failure reason carried while health is RECOVERY_FAILED, for the recovery page to render.",
    )
    backend_outage: BackendOutageObservation | None = Field(
        default=None,
        description=(
            "The machine's backend reported unavailable by a command mngr rejected at the "
            "provider during this episode. Unlike ``last_recovery_error`` a fresh recovery attempt "
            "does not supersede it: it describes the backend rather than the attempt, and the "
            "next attempt is routed through that same backend. Cleared with the record when a "
            "probe observes the machine answering again."
        ),
    )
    connection_failure: ConnectionFailureObservation | None = Field(
        default=None,
        description=(
            "The cause the forward classified for this episode's connection-class failures. Envelopes "
            "repeat at the forward's retry cadence, so this holds one observation per cause rather than "
            "one per envelope; a cause that changes replaces it, except that the residual CONNECT_ERROR "
            "waits for an established cause to fall silent first. Cleared with the record when a probe "
            "observes the machine answering again."
        ),
    )
    is_recovery_a_no_op: bool = Field(
        default=False,
        description=(
            "Whether the start this episode's recovery dispatched reported it booted no host "
            "(``mngr start``'s was_host_started). The machine was up throughout, so it never went "
            "down and came back -- which is why the terminal state reads as the machine not "
            "responding rather than as a restart that failed. Reset by the next recovery attempt "
            "and cleared with the record on recovery."
        ),
    )
    recovery_kind: HostRecoveryKind | None = Field(
        default=None,
        description=(
            "Which recovery the in-flight RECOVERING episode is running. Set when a recovery wins "
            "the RECOVERING transition and read only while RECOVERING -- the recovery card picks "
            "its heading from it (a RESTART reads as 'Restarting <machine>...', a START as the "
            "weaker 'Reconnecting to <machine>...', since a start may well be a no-op). None "
            "outside one."
        ),
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class SystemInterfaceHealthTracker(MutableModel):
    """Per-agent health state machine driven by failure envelopes + probe results.

    Construct one per minds process; share with the envelope-consumer callback
    (which calls ``record_failure``), the background probe loop (which calls
    ``record_probe_success`` / ``record_probe_failure``), the recovery worker
    (``mark_recovering`` / ``mark_recovery_failed`` / ``record_probe_success``),
    and the callback subscribers wired in ``app.py``.
    """

    stuck_threshold_seconds: float = Field(
        default=_DEFAULT_STUCK_THRESHOLD_SECONDS,
        description="Seconds of continuous probe failures before HEALTHY -> STUCK fires.",
    )
    sleep_tracker: SleepTracker | None = Field(
        default=None,
        description=(
            "Records the windows in which this process was not running, so a probe-failure run that "
            "straddles one can be restarted from the wake. None leaves the run purely elapsed-time "
            "based, which is what a process with no heartbeat loop (tests, embedded factories) gets."
        ),
    )
    established_cause_deference_seconds: float = Field(
        default=_DEFAULT_ESTABLISHED_CAUSE_DEFERENCE_SECONDS,
        description=(
            "Seconds a classified connection-failure cause keeps outranking the residual "
            "CONNECT_ERROR without the forward reporting it again (see record_connection_failure)."
        ),
    )
    connection_failure_log_interval_seconds: float = Field(
        default=_DEFAULT_CONNECTION_FAILURE_LOG_INTERVAL_SECONDS,
        description=(
            "Seconds one cause may go unlogged for an agent while it keeps being reported. A "
            "cause that differs from the one last logged is never rationed (see "
            "record_connection_failure)."
        ),
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
    # agent_id_str -> the connection-failure cause last logged for it, and when.
    # Deliberately outside ``_records``: it has to survive the probe success that
    # drops the episode, which is the only thing standing between a repeating
    # failure and one log line per envelope (see record_connection_failure).
    _last_logged_connection_failure: dict[str, tuple[SystemInterfaceBackendFailureReason, datetime]] = PrivateAttr(
        default_factory=dict
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # -- Public callback registration -------------------------------------

    def add_on_change_callback(self, callback: OnChangeCallback) -> None:
        """Register a callback fired whenever any agent's health changes.

        Callbacks receive ``(agent_id, new_health)`` and run on whichever
        thread caused the transition (probe loop or recovery worker).
        Callbacks must be fast and non-blocking; do real work on a queue or
        worker thread.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    def add_on_recovery_callback(self, callback: OnRecoveryCallback) -> None:
        """Register a callback fired on every non-HEALTHY -> HEALTHY transition.

        Distinct from ``add_on_change_callback`` so consumers that only care
        about successful recoveries don't have to filter the firehose of
        every state change.
        """
        with self._lock:
            self._on_recovery_callbacks.append(callback)

    def add_on_stuck_edge_callback(self, callback: OnStuckEdgeCallback) -> None:
        """Register a callback fired only on the probe-confirmed HEALTHY -> STUCK edge.

        Narrower than :meth:`add_on_change_callback` on purpose: only this edge
        means "an outage just started, nothing has been tried yet", and it fires
        exactly once per outage episode, so a consumer that dispatches work (the
        unattended start) needs no latch of its own.

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

    def _is_run_interrupted_by_sleep_locked(self, record: _AgentRecord) -> bool:
        """Whether this agent's in-progress failure run straddles a sleep (must hold ``self._lock``).

        Asks the wall-clock question, because the monotonic onset the run is
        measured against cannot answer it: that clock freezes across sleep, so
        the run's own elapsed time looks the same whether the machine slept or
        the workspace really was failing that whole time.

        Called under this tracker's lock, which is safe because the tracker it
        calls fires its own callbacks outside its lock -- so there is no path by
        which a sleep-tracker consumer re-enters here holding it.
        """
        if self.sleep_tracker is None or record.failure_run_started_wall_at is None:
            return False
        return self.sleep_tracker.was_asleep_since(record.failure_run_started_wall_at)

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
        start endpoint (the machines-list click-through dispatches a start), and
        those machines would otherwise stay suppressed for the life of the
        process.

        ``is_stop_in_flight`` covers the one window in which that probe clear
        would be wrong: a stop's own command, which blocks for tens of seconds
        (a cloud host, minutes) while the interface goes on answering for the
        first of them. A machine being stopped is often a probe target already
        (suspect-enrolled, STUCK, or RECOVERY_FAILED), so that 200 gets taken,
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

    def record_connection_failure(
        self,
        agent_id: AgentId,
        reason: SystemInterfaceBackendFailureReason,
        detail: str | None,
    ) -> None:
        """Record what the forward classified this episode's connection failures as.

        Called alongside :meth:`record_failure` for every connection-class
        envelope, which the forward re-emits on each retry -- roughly once a
        second for as long as the page keeps polling. So this holds one
        observation per *cause*: a repeat of the cause already held only
        refreshes when it was last seen, and a different cause replaces it,
        which is the only case worth a log line.

        With one exception: ``CONNECT_ERROR`` is the residual class -- it means
        the cause was not established -- so it does not displace a cause that
        still is. Without that rule the surfaces flap, because an episode
        produces envelopes from several request paths at once: a pooled HTTP
        request can report ``POOL_EXHAUSTED`` while a websocket handshake
        against the same machine reports ``CONNECT_ERROR``, and at ~1 Hz the
        device-side card would appear and disappear once a second.

        "Still is" is the whole of that exception, and why the held cause carries
        when it was last reported. The forward keeps reporting a cause that is
        still happening, so one that has gone quiet for
        ``established_cause_deference_seconds`` has stopped happening -- and
        both device-side causes are things that stop: a pool refills, a socket
        that would not bind binds on the next try. Deferring to such a cause
        indefinitely would pin a momentary fault on this device over an outage
        that has since become the machine's, telling the user their machine is
        probably fine and withholding the start that would fix it. That is the
        misdiagnosis this whole decomposition exists to end, only inverted, so
        the residual reason takes over once the specific one falls silent.

        That log line is also the Sentry breadcrumb: a bug report from a user
        whose device could not reach a healthy workspace has to carry which of
        the three causes it was, or the split is unmeasurable in the field. Which
        is why it is rationed separately from the record, against a mark that a
        probe success does not drop: the record holds one observation per cause
        per *episode*, and a failure that is not the system interface's own
        outlives any number of episodes -- the machine answers every probe while
        some other service on it refuses every request. A repeat of the cause
        last logged waits out ``connection_failure_log_interval_seconds``; a
        different cause is logged at once, because that is the transition worth
        seeing.

        Does not change health, exactly as :meth:`record_failure` does not -- the
        probe loop remains the only authority on whether the workspace is
        reachable. The record is dropped with the rest of the episode's state
        when a probe finds the machine answering again.
        """
        aid_str = str(agent_id)
        now = datetime.now(timezone.utc)
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            existing = record.connection_failure
            if existing is not None and existing.reason == reason:
                # The same cause, again. Its first ``detail`` is kept: repeats of
                # one cause quote near-identical text, and rewriting it every
                # second would churn what the card shows for no new information.
                record.connection_failure = ConnectionFailureObservation(
                    reason=existing.reason, detail=existing.detail, last_observed_at=now
                )
                return
            if (
                existing is not None
                and reason == SystemInterfaceBackendFailureReason.CONNECT_ERROR
                and (now - existing.last_observed_at).total_seconds() < self.established_cause_deference_seconds
            ):
                return
            record.connection_failure = ConnectionFailureObservation(
                reason=reason, detail=detail, last_observed_at=now
            )
            last_logged = self._last_logged_connection_failure.get(aid_str)
            is_worth_logging = (
                last_logged is None
                or last_logged[0] != reason
                or (now - last_logged[1]).total_seconds() >= self.connection_failure_log_interval_seconds
            )
            if is_worth_logging:
                self._last_logged_connection_failure[aid_str] = (reason, now)
        if is_worth_logging:
            logger.info(
                "System-interface connection failure for {} classified as {}{}",
                agent_id,
                reason.value,
                f": {detail}" if detail else "",
            )

    def get_connection_failure(self, agent_id: AgentId) -> ConnectionFailureObservation | None:
        """Return this episode's classified connection-failure cause for ``agent_id``, or None.

        Returned as observed, not as a verdict: it says what the forward saw, and
        a caller deciding what to show the user weighs it against everything else
        the episode has produced.
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return None
            return record.connection_failure

    def record_probe_failure(self, agent_id: AgentId) -> None:
        """Record that a background probe observed ``agent_id`` as unreachable.

        Starts the agent's probe-failure run on the first failure, then
        transitions HEALTHY -> STUCK once the run has lasted at least
        ``stuck_threshold_seconds``. Probe failures for an agent that is not
        HEALTHY (already STUCK, or RECOVERING / RECOVERY_FAILED -- states owned
        by the recovery flow) or that has no record (de-enrolled concurrently)
        are ignored.

        A run whose onset predates a recorded sleep interval is restarted from
        this failure instead of being continued: the probe loop was frozen for
        the sleep, so those seconds were never observed and cannot be counted
        toward a conviction. Only the run is reset -- the *episode* onset
        (``outage_started_wall_at``) is left alone, since the machine really did
        stop answering when it did, and the discovery-freshness gate that reads
        it is only made stricter by an older mark.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        stuck_after_seconds: float | None = None
        is_run_restarted_at_wake = False
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
            now_wall = datetime.now(timezone.utc)
            # The run starts here in two cases: this is the first failure of a
            # new one, or the one in progress spans seconds nobody observed.
            is_run_restarted_at_wake = (
                record.failure_run_started_at is not None and self._is_run_interrupted_by_sleep_locked(record)
            )
            if record.failure_run_started_at is None or is_run_restarted_at_wake:
                record.failure_run_started_at = now
                record.failure_run_started_wall_at = now_wall
            # Opens the episode too, if this is the failure that started it. A
            # later run within the same episode cannot reach here (that needs
            # HEALTHY), so the earliest failure keeps the mark -- and a run
            # restarted at a wake leaves it exactly where it was.
            if record.outage_started_wall_at is None:
                record.outage_started_wall_at = now_wall
            elapsed = now - record.failure_run_started_at
            if elapsed + 1e-6 >= self.stuck_threshold_seconds:
                record.health = AgentHealth.STUCK
                fire_health = AgentHealth.STUCK
                stuck_after_seconds = elapsed
        # A restarted run is the sleep signal actually changing an outcome, and
        # the only trace of a conviction that did not happen.
        if is_run_restarted_at_wake:
            logger.info(
                "Probe-failure run for {} restarted: it began before a recorded sleep interval, "
                "so the stuck threshold re-accumulates from now",
                agent_id,
            )
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
        STUCK, RECOVERING, or RECOVERY_FAILED, transitions it back to HEALTHY and
        fires on-change. The now-clean record is dropped so ``_records`` stays
        scoped to agents that still need attention.

        Called by the background probe loop on a 200, and by the recovery worker
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

    def mark_recovering(self, agent_id: AgentId, kind: HostRecoveryKind) -> bool:
        """Mark ``agent_id`` as RECOVERING (called from the recovery dispatch).

        Clears any in-progress probe-failure run (the agent is already
        known-bad) and fires on-change so the recovery page can re-label.
        ``kind`` records which recovery is running so the recovery page can name
        the wait; it is recorded only when this call wins the transition, so a
        deduped later request never rewrites the kind of the episode already in
        flight.

        Returns whether this call transitioned the agent into RECOVERING (it
        was not already RECOVERING). That reports the transition; it does not
        decide who owns the recovery. Ownership is the workspace's single
        operation slot, which ``dispatch_host_recovery`` wins before it gets
        here -- a tracker-side compare-and-set could not serialize against the
        backup operations, which never touch the tracker.
        """
        aid_str = str(agent_id)
        fire_health: AgentHealth | None = None
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            record.failure_run_started_at = None
            record.failure_run_started_wall_at = None
            # A fresh recovery attempt supersedes any prior failure reason, and
            # any prior attempt's account of whether it booted anything.
            record.last_recovery_error = None
            record.is_recovery_a_no_op = False
            if record.health != AgentHealth.RECOVERING:
                record.health = AgentHealth.RECOVERING
                record.recovery_kind = kind
                fire_health = AgentHealth.RECOVERING
        if fire_health is not None:
            self._fire_on_change(agent_id, fire_health)
        return fire_health is not None

    def mark_recovery_failed(self, agent_id: AgentId, error: str) -> None:
        """Mark ``agent_id`` as RECOVERY_FAILED, carrying ``error`` as the reason.

        Called when a recovery fails to bring the workspace back within its
        window, or its ``mngr`` commands error out. The reason is surfaced to
        the recovery page so it can render an escalate / try-again affordance
        instead of an indefinite wait.
        """
        aid_str = str(agent_id)
        with self._lock:
            record = self._records.setdefault(aid_str, _AgentRecord())
            record.failure_run_started_at = None
            record.failure_run_started_wall_at = None
            record.last_recovery_error = error
            # Always re-fire: a second failure with a new reason must reach
            # the recovery page even if the state is already RECOVERY_FAILED.
            record.health = AgentHealth.RECOVERY_FAILED
        self._fire_on_change(agent_id, AgentHealth.RECOVERY_FAILED)

    def get_health(self, agent_id: AgentId) -> AgentHealth:
        """Return the current health for ``agent_id`` (HEALTHY by default)."""
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return AgentHealth.HEALTHY
            return record.health

    def get_last_recovery_error(self, agent_id: AgentId) -> str | None:
        """Return the failure reason for ``agent_id`` if it is RECOVERY_FAILED."""
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return None
            return record.last_recovery_error

    def record_recovery_started_nothing(self, agent_id: AgentId) -> None:
        """Record that this episode's dispatched ``mngr start`` reported it booted no host.

        ``mngr start`` is idempotent, so a start against a host that was already
        running leaves it alone -- and that is affirmative evidence about what
        the user saw: the machine stayed up throughout, so whatever is still
        wrong with it, it was not taken down and brought back. The surfaces read
        this to say the machine is not responding instead of claiming a failed
        restart of it.

        Says nothing about the agent the start named: one whose session had died
        is relaunched whether or not a host was booted. Only the host is settled
        here, which is the half the copy turns on.
        """
        with self._lock:
            self._records.setdefault(str(agent_id), _AgentRecord()).is_recovery_a_no_op = True

    def is_recovery_a_no_op(self, agent_id: AgentId) -> bool:
        """Whether this episode's dispatched start reported it booted nothing.

        False when no start has reported yet, which is also the honest default:
        without a report there is no evidence either way, and the restart framing
        is what the surfaces already use.
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            return record is not None and record.is_recovery_a_no_op

    def record_backend_outage(self, agent_id: AgentId, provider_name: str, reason: str) -> None:
        """Record that ``provider_name`` rejected a command for ``agent_id`` as unavailable.

        Called from the recovery worker, which is where minds first runs a command
        against a machine whose backend has gone down. Recording it is what lets
        the recovery surfaces name the backend on the same edge that raises them,
        rather than a provider poll later.
        """
        with self._lock:
            record = self._records.setdefault(str(agent_id), _AgentRecord())
            record.backend_outage = BackendOutageObservation(
                provider_name=provider_name, reason=reason, observed_at=datetime.now(timezone.utc)
            )

    def get_backend_outage(self, agent_id: AgentId) -> BackendOutageObservation | None:
        """Return the backend outage observed in-band for ``agent_id``, or None.

        Returned as observed, with the moment attached: this is the tracker
        remembering what a command reported, not a verdict. A caller deciding
        whether it still describes the backend *now* must weigh it against what
        discovery has reported since -- see ``_recorded_backend_outage_reason``
        in ``workspace_recovery``, which is the one place that does.
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return None
            return record.backend_outage

    def get_recovery_kind(self, agent_id: AgentId) -> HostRecoveryKind | None:
        """Return which recovery is in flight, or None if not RECOVERING.

        Only meaningful while the agent is RECOVERING; returns None otherwise so
        a stale value from a prior episode is never read. The recovery card picks
        its heading from it: a RESTART reads as "Restarting <machine>...", a
        START (which may be a no-op) as the weaker "Reconnecting to <machine>...".
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None or record.health != AgentHealth.RECOVERING:
                return None
            return record.recovery_kind

    def get_failure_run_started_wall_at(self, agent_id: AgentId) -> datetime | None:
        """Return the wall-clock (UTC) start of the current probe-failure run, or None.

        The run begins on the first failed probe and ends at the next recovery
        attempt, which clears it: the machine is already known-bad, so the run
        has nothing left to decide. A caller asking when the *outage* began wants
        :meth:`get_outage_started_wall_at`, which spans those attempts.
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return None
            return record.failure_run_started_wall_at

    def get_outage_started_wall_at(self, agent_id: AgentId) -> datetime | None:
        """Return the wall-clock (UTC) moment this machine stopped answering, or None.

        The outage onset: the first probe failure of the current unhealthy
        episode, held until a probe observes the machine answering again. Recovery
        compares discovery snapshots against it to decide whether what the
        resolver reports describes *this* outage or the world before it, so it
        must span the episode -- a recovery attempt part-way through does not make
        pre-outage evidence current, and the failure *run* is cleared by exactly
        that. None when the machine is healthy, or was force-marked STUCK with no
        probe-failure run behind it.
        """
        with self._lock:
            record = self._records.get(str(agent_id))
            if record is None:
                return None
            return record.outage_started_wall_at

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
        RECOVERY_FAILED -- the loop polls those for recovery. HEALTHY
        non-suspect agents are omitted; probing every workspace unconditionally
        would scale probe traffic with workspace count for no benefit.

        RECOVERING agents are deliberately excluded, whichever recovery is
        running: the worker owns that decision via its own
        ``_await_system_interface_ready`` probe, which runs once the commands
        return, so a background probe alongside it is a second opinion on a
        question already being answered.

        A RESTART is where answering it early does real damage. Its ``mngr
        stop`` takes tens of seconds to tear the backend down, and the *old*
        system interface goes on answering 200 for the first of them -- so a
        probe in the window between ``mark_recovering`` and that teardown would
        flip the agent back to HEALTHY (via ``record_probe_success``), causing
        the recovery page to 302 the user back into a workspace that is about
        to disappear.
        """
        with self._lock:
            return frozenset(
                AgentId(aid)
                for aid, record in self._records.items()
                if (record.is_suspect and record.health == AgentHealth.HEALTHY)
                or record.health in (AgentHealth.STUCK, AgentHealth.RECOVERY_FAILED)
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


class BackendFailureRecorder(FrozenModel):
    """Routes one ``system_interface_backend_failure`` envelope into the tracker.

    The plugin observes; this is the whole of minds' policy on what to do with
    an observation, in one place so the enrollment decision and the cause record
    cannot drift apart. Registered as the consumer's failure callback in
    ``minds run``.

    Enrollment and cause-recording are independent questions and are asked
    separately: a 503 enrolls but names no cause, and a connection-class failure
    does both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    tracker: SystemInterfaceHealthTracker = Field(description="Tracker the observation is recorded on")

    def __call__(
        self,
        agent_id: AgentId,
        reason: SystemInterfaceBackendFailureReason,
        status_code: int | None,
        detail: str | None,
    ) -> None:
        if not should_enroll_suspect_for_backend_failure(reason, status_code):
            return
        if reason in _CONNECTION_CLASS_REASONS:
            self.tracker.record_connection_failure(agent_id, reason, detail)
        self.tracker.record_failure(agent_id)
