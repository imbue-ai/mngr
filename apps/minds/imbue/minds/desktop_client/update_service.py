"""Dispatches update runs and closes them out; the one path both the routes and the scheduler use.

The liveness poll here is the app's only reader of an in-flight run: it moves the row and lands the verdict.
"""

import threading
from collections.abc import Callable
from enum import auto
from typing import Final
from typing import Protocol

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_GATE_PROBE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import GATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import build_workspace_script_command
from imbue.minds.desktop_client.backup_workspace_scripts import extract_marker_json
from imbue.minds.desktop_client.skill_chat import SkillSupport
from imbue.minds.desktop_client.skill_chat import check_skill_support
from imbue.minds.desktop_client.skill_chat import generate_chat_name
from imbue.minds.desktop_client.skill_chat import spawn_skill_chat
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.update_apply_window import UpdateAgentLiveness
from imbue.minds.desktop_client.update_apply_window import UpdateApplyWindowManager
from imbue.minds.desktop_client.update_chat import UPDATE_SKILL_NAME
from imbue.minds.desktop_client.update_chat import build_update_chat_message
from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleStore
from imbue.minds.desktop_client.update_scheduler import ScheduledRunConditions
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateDetector
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

_GATE_PROBE_TIMEOUT_SECONDS: Final[float] = 60.0

# One warm ``mngr exec`` per in-flight run per pass; tight enough that a dead
# agent's row unlocks in seconds rather than at the detection sweep's next wake.
_RUN_POLL_INTERVAL_SECONDS: Final[float] = 20.0

# A lifecycle read can catch an agent idle at a turn boundary, so one WAITING
# sample is not evidence.
_WAITING_POLLS_BEFORE_SURFACING: Final[int] = 2

# APPLYING is polled too: the probe runs over ``mngr exec``, which works while
# the interface is down, and each sighting keeps the apply window extended.
_POLLED_ACTIVITIES: Final[frozenset[UpdateActivity]] = frozenset(
    {UpdateActivity.RUNNING, UpdateActivity.WAITING, UpdateActivity.APPLYING}
)

# REFUSED counts: retrying it unwatched tomorrow would just repeat. ALREADY_CURRENT
# does not: a no-op is the expected outcome for a stale label or an unreadable version.
_FAILURE_VERDICTS: Final[frozenset[UpdateVerdict]] = frozenset(
    {UpdateVerdict.STUCK, UpdateVerdict.REFUSED, UpdateVerdict.NEEDS_RECREATION}
)


class UpdateDispatchOutcome(UpperCaseStrEnum):
    """What a call to :meth:`WorkspaceUpdateService.dispatch_update` did."""

    DISPATCHED = auto()
    ALREADY_RUNNING = auto()
    UNSUPPORTED = auto()
    """The workspace predates the update-self skill; there is nothing to run."""
    UNREACHABLE = auto()
    """The workspace could not be probed or started."""
    SPAWN_FAILED = auto()


class OnUpdateRunFinishedCallback(Protocol):
    """Told when a run ends; ``is_real_failure`` is a stall or a short-ending verdict."""

    def __call__(self, agent_id: AgentId, *, is_real_failure: bool) -> None: ...


class WorkspaceUpdateService(MutableModel):
    """Dispatches updates and closes them out; the routes' and scheduler's shared engine."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    state_store: WorkspaceUpdateStateStore = Field(frozen=True, description="Detection + run facts.")
    schedule_store: UpdateScheduleStore = Field(frozen=True, description="Armed scheduled-update intents.")
    detector: WorkspaceUpdateDetector = Field(frozen=True, description="Re-read a version after a run.")
    apply_window: UpdateApplyWindowManager = Field(frozen=True, description="Probes whether a run is still alive.")
    mngr_caller: MngrCaller = Field(frozen=True, description="Runs every in-workspace command.")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Discovery: hosts and their states.")
    start_workspace: Callable[[AgentId], bool] = Field(
        frozen=True,
        description=(
            "Brings a workspace's host up before the run, returning whether it is up. The shared host "
            "lifecycle action, not a bare ``mngr start``: an update that wakes a machine the app had "
            "stopped must clear its unattended-recovery suppression, or the machine stays unrecoverable "
            "for the rest of the session."
        ),
    )
    paths: InstallationPaths | None = Field(
        default=None, frozen=True, description="Workspace data paths; None when backups are not configurable at all."
    )
    # agent_id_str -> consecutive WAITING polls; only the poll thread touches it.
    _waiting_streak_by_agent: dict[str, int] = PrivateAttr(default_factory=dict)
    _poll_stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _is_polling_started: bool = PrivateAttr(default=False)
    _on_run_finished_callbacks: list[OnUpdateRunFinishedCallback] = PrivateAttr(default_factory=list)
    _callbacks_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def add_on_run_finished_callback(self, callback: OnUpdateRunFinishedCallback) -> None:
        """Register a callback told when a run ends, however it ended.

        A registration rather than a field because the scheduler that wants it
        is built FROM this service's bound methods, so it exists only afterwards.
        """
        with self._callbacks_lock:
            self._on_run_finished_callbacks.append(callback)

    # -- Dispatch ---------------------------------------------------------

    def dispatch_update(self, agent_id: AgentId, *, target_override: str | None = None) -> UpdateDispatchOutcome:
        """Start an update run in ``agent_id``, returning what happened.

        ``target_override`` was confirmed at the app's version field when it was
        chosen, whether the run goes out now or from a schedule.

        The run slot is claimed before any remote work, so a concurrent dispatch
        loses rather than starting a second update. Every non-dispatch exit
        releases it in ``finally``: a row left on STARTING is never polled, so
        nothing but an app restart could unlock it.
        """
        chat_name = generate_chat_name(UPDATE_SKILL_NAME)
        if not self.state_store.try_begin_run(
            agent_id, chat_agent_name=chat_name, target_override=target_override or ""
        ):
            return UpdateDispatchOutcome.ALREADY_RUNNING
        is_dispatched = False
        try:
            outcome = self._start_claimed_run(agent_id, chat_name, target_override)
            is_dispatched = outcome is UpdateDispatchOutcome.DISPATCHED
            return outcome
        finally:
            # Conditional on STARTING: a spawn that timed out after creating the
            # chat has a live run the sweep may already have entered as RUNNING,
            # and writing IDLE over it would let a retry start a second update.
            if not is_dispatched:
                self.state_store.set_activity(
                    agent_id, UpdateActivity.IDLE, only_from=frozenset({UpdateActivity.STARTING})
                )

    def _start_claimed_run(
        self, agent_id: AgentId, chat_name: str, target_override: str | None
    ) -> UpdateDispatchOutcome:
        """Do the work of a dispatch that has already won the run slot."""
        # A start no-ops against a running host, so this is unconditional rather
        # than gated on a possibly-stale discovery answer.
        if not self.start_workspace(agent_id):
            return UpdateDispatchOutcome.UNREACHABLE
        support = check_skill_support(self.mngr_caller, agent_id, UPDATE_SKILL_NAME)
        match support:
            case SkillSupport.UNSUPPORTED:
                return UpdateDispatchOutcome.UNSUPPORTED
            case SkillSupport.UNREACHABLE:
                return UpdateDispatchOutcome.UNREACHABLE
            case SkillSupport.SUPPORTED:
                pass
        if not spawn_skill_chat(
            self.mngr_caller,
            agent_id,
            chat_name=chat_name,
            # Read here rather than carried from the press: a schedule armed days ago is not
            # evidence about the backups this run is actually about to go without.
            message=build_update_chat_message(
                target_override=target_override, is_backup_configured=self.is_backup_configured(agent_id)
            ),
        ):
            return UpdateDispatchOutcome.SPAWN_FAILED
        self.state_store.set_activity(agent_id, UpdateActivity.RUNNING)
        return UpdateDispatchOutcome.DISPATCHED

    def dispatch_for_scheduler(self, agent_id: AgentId, target_ref: str) -> bool:
        """The scheduler's dispatch hook: whether the run went out."""
        outcome = self.dispatch_update(agent_id, target_override=target_ref or None)
        if outcome is not UpdateDispatchOutcome.DISPATCHED:
            logger.info("Scheduled update for {} did not dispatch: {}", agent_id, outcome.value)
        return outcome is UpdateDispatchOutcome.DISPATCHED

    # -- Scheduling -------------------------------------------------------

    def is_backup_configured(self, agent_id: AgentId) -> bool:
        """Whether this workspace has a canonical restic env, i.e. backups to fall back on.

        A presence check only: this rides every update frame, and the env's contents are a password.
        """
        return self.paths is not None and has_canonical_env(self.paths, agent_id)

    def read_conditions(self, agent_id: AgentId) -> ScheduledRunConditions:
        """Gather the facts one scheduled attempt is decided on."""
        state = self.state_store.get(agent_id)
        host_state = self.read_host_state(agent_id)
        return ScheduledRunConditions(
            is_offered=state.is_update_dispatchable,
            is_run_in_flight=state.is_run_in_flight,
            is_reachable=host_state is not None,
            # An offline host has nothing running in it, and the gate probe execs
            # with ``--no-start``, so asking would read as busy.
            is_quiet=self.is_workspace_quiet(agent_id) if host_state is HostState.RUNNING else True,
        )

    def read_host_state(self, agent_id: AgentId) -> HostState | None:
        """The workspace's host state per discovery, or None when it is unknown to us."""
        info = self.backend_resolver.get_agent_display_info(agent_id)
        if info is None or info.host_id is None:
            return None
        try:
            host_id = HostId(info.host_id)
        except ValueError:
            # Static / in-memory resolvers report a placeholder host id.
            return None
        return self.backend_resolver.get_host_state(host_id)

    def is_workspace_quiet(self, agent_id: AgentId) -> bool:
        """Whether no chats are running in the workspace, per the backup flow's gate probe.

        An unanswered probe counts as busy: bouncing the services under a chat is the expensive mistake.
        """
        command = build_workspace_script_command(BACKUP_GATE_PROBE_SCRIPT, ("--agent-id", str(agent_id)))
        result = self.mngr_caller.call(
            ["exec", "--agent", str(agent_id), command, "--no-start"], timeout=_GATE_PROBE_TIMEOUT_SECONDS
        )
        payload = extract_marker_json(result.stdout, GATE_RESULT_MARKER)
        if payload is None:
            logger.info("Could not read the chat gate for {}; treating it as busy", agent_id)
            return False
        gate_error = str(payload.get("gate_error") or "")
        if gate_error:
            logger.info("The chat gate for {} could not list chats ({}); treating it as busy", agent_id, gate_error)
            return False
        running_chats = payload.get("running_chats")
        return not (isinstance(running_chats, list) and running_chats)

    # -- Closing a run out ------------------------------------------------

    def handle_verdict(self, agent_id: AgentId, verdict: UpdateVerdict, resulting_ref: str) -> None:
        """Everything the app owes a terminal verdict: re-detect, report, unschedule."""
        # The badge is stale the instant an update lands; drop the cached read and sweep now.
        self.detector.invalidate_cached_version(agent_id)
        self.detector.request_pass()
        is_failure = verdict in _FAILURE_VERDICTS
        if is_failure:
            # Error level so this reaches Sentry; the run's reasoning stays in the user's chat transcript.
            logger.error(
                "Update run in machine {} ended {}: resulting={}",
                agent_id,
                verdict.value,
                resulting_ref or "?",
            )
        else:
            logger.info(
                "Update run in machine {} ended {}: now on {}",
                agent_id,
                verdict.value,
                resulting_ref or "?",
            )
        self._close_scheduled_run_out(agent_id, is_real_failure=is_failure)

    def _close_scheduled_run_out(self, agent_id: AgentId, *, is_real_failure: bool) -> None:
        """Tell the listeners a run ended (the scheduler stops a machine it started and does not retry a failure unwatched)."""
        with self._callbacks_lock:
            callbacks = list(self._on_run_finished_callbacks)
        for callback in callbacks:
            callback(agent_id, is_real_failure=is_real_failure)

    def poll_in_flight_runs(self) -> None:
        """One pass over every in-flight run, one probe each.

        Authority order: a verdict naming this run's chat ends it; apply fields
        open or extend the window; a recorded hold goes WAITING at once with its
        detail; otherwise the agent's lifecycle state decides (gone is STALLED,
        idle for consecutive polls is WAITING, moving again is RUNNING). An
        unanswered probe moves nothing.

        STARTING is not probed: the chat may not exist yet, and STALLED is terminal.
        """
        snapshot = self.state_store.snapshot()
        for stale_id_str in set(self._waiting_streak_by_agent) - set(snapshot):
            del self._waiting_streak_by_agent[stale_id_str]
        # Every write below is an ``only_from`` transition: a verdict landing
        # during the probe postdates this pass's reading.
        for agent_id_str, state in snapshot.items():
            if state.activity not in _POLLED_ACTIVITIES:
                self._waiting_streak_by_agent.pop(agent_id_str, None)
                continue
            self._poll_one_run(AgentId(agent_id_str), state)

    def _poll_one_run(self, agent_id: AgentId, state: UiWorkspaceUpdate) -> None:
        aid_str = str(agent_id)
        probe = self.apply_window.probe_run(agent_id)
        status = probe.run_status
        is_records_run = (
            status is not None and bool(state.chat_agent_name) and status.chat_agent_name == state.chat_agent_name
        )
        if is_records_run:
            assert status is not None
            # The run's own record outranks the claim: its start is what the sweep dedups the file by,
            # its hold is what the modal quotes, and its verdict ends the run.
            self.state_store.adopt_run_record(agent_id, status)
            if status.verdict is not None:
                self._waiting_streak_by_agent.pop(aid_str, None)
                self.apply_window.close_window(agent_id, reason="terminal verdict")
                self.handle_verdict(agent_id, status.verdict, status.resulting_ref)
                return
        if probe.is_apply_in_progress:
            # Windowing on the poll's sighting means a normal apply is windowed
            # before its reveal ever fails a probe.
            self._waiting_streak_by_agent.pop(aid_str, None)
            self.apply_window.open_window(agent_id, apply_updated_at=probe.apply_updated_at)
            return
        if is_records_run and status is not None and status.is_holding:
            # No debounce for a recorded hold.
            self._waiting_streak_by_agent.pop(aid_str, None)
            if self.state_store.set_activity(
                agent_id, UpdateActivity.WAITING, only_from=frozenset({UpdateActivity.RUNNING})
            ):
                logger.info("The update in machine {} is holding for the user; saying so", agent_id)
            return
        if not probe.is_run_alive:
            self._waiting_streak_by_agent.pop(aid_str, None)
            if self.state_store.set_activity(agent_id, UpdateActivity.STALLED, only_from=_POLLED_ACTIVITIES):
                logger.info("The update run in machine {} is gone with no verdict; unlocking the row", agent_id)
                # A gone agent ends a scheduled run as much as a verdict does.
                self._close_scheduled_run_out(agent_id, is_real_failure=True)
        elif probe.is_probe_answered and probe.is_agent_waiting:
            streak = self._waiting_streak_by_agent.get(aid_str, 0) + 1
            self._waiting_streak_by_agent[aid_str] = streak
            # Under the surfacing threshold the streak accrues and the row stays as it is; a hold the
            # record has since cleared already came off with the record's adoption above.
            if streak >= _WAITING_POLLS_BEFORE_SURFACING and self.state_store.set_activity(
                agent_id, UpdateActivity.WAITING, only_from=frozenset({UpdateActivity.RUNNING})
            ):
                logger.info("The update agent in machine {} is waiting in its chat; saying so", agent_id)
        elif probe.is_probe_answered and probe.agent_liveness is UpdateAgentLiveness.ALIVE:
            self._waiting_streak_by_agent.pop(aid_str, None)
            # Post-apply, the agent is composing its results; the window is left
            # to close on its own (a verdict, the tracker's probe, or its deadline).
            self.state_store.set_activity(
                agent_id,
                UpdateActivity.RUNNING,
                only_from=frozenset({UpdateActivity.WAITING, UpdateActivity.APPLYING}),
            )
        else:
            # An unanswered probe is not evidence; the row and its streak stay put.
            pass

    def start_run_polling(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the liveness-poll strand. Idempotent."""
        if self._is_polling_started:
            return
        self._is_polling_started = True
        concurrency_group.start_new_thread(
            target=self._run_poll_loop,
            args=(concurrency_group,),
            name="update-run-poll",
            daemon=True,
            # The loop logs its own failures; a crash must not poison the root group.
            is_checked=False,
        )

    def stop_run_polling(self) -> None:
        """Stop the liveness-poll strand (it exits at its next wake)."""
        self._poll_stop_event.set()

    def _run_poll_loop(self, concurrency_group: ConcurrencyGroup) -> None:
        # Waits on the group's shutdown event so a quit does not wait out an interval.
        while not concurrency_group.shutdown_event.wait(timeout=_RUN_POLL_INTERVAL_SECONDS):
            if self._poll_stop_event.is_set():
                break
            try:
                self.poll_in_flight_runs()
            except (MindError, MngrError, OSError, RuntimeError, ValueError) as e:
                # One unprobeable machine must not end polling for the rest;
                # anything outside this set is a bug and kills the strand.
                logger.opt(exception=e).error("An update run-liveness poll failed; continuing")
        logger.debug("Exited the update run-liveness poll loop")
