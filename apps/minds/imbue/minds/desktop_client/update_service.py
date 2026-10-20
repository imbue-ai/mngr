"""Starts, schedules, and closes out update runs; every action is one run of the workspace's update command.

The workspace owns its schedule: arming writes a file and a cron line into it, and its own
dispatcher starts the run inside the window. The liveness poll here is the app's only reader of
an in-flight run: it moves the row and lands the verdict.
"""

import shlex
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from typing import Final
from typing import Protocol
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.in_workspace_mngr import FAILURE_DETAIL_MAX_CHARS
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_command
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.update_apply_window import UpdateAgentLiveness
from imbue.minds.desktop_client.update_apply_window import UpdateApplyWindowManager
from imbue.minds.desktop_client.update_status import FAILURE_UPDATE_VERDICTS
from imbue.minds.desktop_client.update_status import LANDED_UPDATE_VERDICTS
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateArmOutcome
from imbue.minds.desktop_client.update_status import UpdateDispatchFailure
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.update_workspace_command import UpdateCommandCall
from imbue.minds.desktop_client.update_workspace_command import UpdateCommandReach
from imbue.minds.desktop_client.update_workspace_command import UpdateCommandResult
from imbue.minds.desktop_client.update_workspace_command import UpdateWorkspaceFacts
from imbue.minds.desktop_client.update_workspace_command import UpdateWorkspaceSettings
from imbue.minds.desktop_client.update_workspace_command import arm_update
from imbue.minds.desktop_client.update_workspace_command import cancel_update_schedule
from imbue.minds.desktop_client.update_workspace_command import parse_update_command_result
from imbue.minds.desktop_client.update_workspace_command import parse_update_workspace_facts
from imbue.minds.desktop_client.update_workspace_command import read_update_facts
from imbue.minds.desktop_client.update_workspace_command import start_update_now
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateDetector
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId

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

# A run's own record starts after the app's claim, give or take the gap between the laptop's
# clock and the workspace's; one under the run's chat name that started earlier than this is an
# earlier run's, whose chat had the same name (update chats are named for their target).
_EARLIER_RUN_RECORD_MARGIN: Final[timedelta] = timedelta(minutes=5)

# The new chat's page opens with the provider chooser up (the chat app's ``open=providers`` key).
_SIGN_IN_CHAT_PATH: Final[str] = "/?open=providers"

# The window opens only once the user's client is connected to the workspace, which the
# press that asked for it has just navigated into; the workspace's layout CLI refuses until then.
_SIGN_IN_OPEN_ATTEMPTS: Final[int] = 15
_SIGN_IN_OPEN_INTERVAL_SECONDS: Final[int] = 2
_SIGN_IN_OPENED_SENTINEL: Final[str] = "MINDS_SIGN_IN_CHAT_OPENED"
_SIGN_IN_TIMEOUT_SECONDS: Final[float] = 90.0


class UpdateDispatchOutcome(UpperCaseStrEnum):
    """What a call to :meth:`WorkspaceUpdateService.dispatch_update` did."""

    DISPATCHED = auto()
    ALREADY_RUNNING = auto()
    """An update was already under way there; the row now reports that one."""
    NEEDS_SIGN_IN = auto()
    """The workspace has no signed-in AI account its update chat could run on."""
    UNSUPPORTED = auto()
    """The workspace predates the update-self skill; there is nothing to run."""
    START_FAILED = auto()
    """The command could not reach the workspace, and its host would not start; the start says why."""
    UNREACHABLE = auto()
    """The command could not reach the workspace, even after it was started."""
    SPAWN_FAILED = auto()
    """The workspace could not create the update chat; its words say why."""
    UNCONFIRMED = auto()
    """The command was cut off in the workspace, so whether the chat was made is unknown.

    A chat that was made records its run, which the sweep adopts.
    """


# What each outcome that did not dispatch tells the user; the route answers with it
# and the run slice records it, so the modal says the same thing whether it stayed open.
DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME: Final[dict[UpdateDispatchOutcome, str]] = {
    UpdateDispatchOutcome.ALREADY_RUNNING: "An update is already running in this workspace.",
    UpdateDispatchOutcome.NEEDS_SIGN_IN: "Sign in to an AI account in this workspace, then try again.",
    UpdateDispatchOutcome.UNSUPPORTED: (
        "This workspace is too old to update itself. Ask an agent inside it for help, "
        "or create a new workspace and migrate your work."
    ),
    UpdateDispatchOutcome.START_FAILED: "Couldn't start this workspace to run the update.",
    UpdateDispatchOutcome.UNREACHABLE: "Couldn't reach this workspace to start the update.",
    UpdateDispatchOutcome.SPAWN_FAILED: "Couldn't start the update agent in this workspace.",
    UpdateDispatchOutcome.UNCONFIRMED: (
        "Couldn't confirm the update started in this workspace. If it did, it will show here shortly."
    ),
}

# Recorded for a dispatch that raised rather than returned: no outcome names what went wrong.
_DISPATCH_RAISED_FAILURE: Final[UpdateDispatchFailure] = UpdateDispatchFailure(
    message="Something went wrong starting the update in this workspace."
)


class UpdateDispatch(FrozenModel):
    """What a dispatch did, and what the workspace, mngr, or the host start said when it did not go out."""

    outcome: UpdateDispatchOutcome = Field(description="What the dispatch did")
    failure_detail: str = Field(
        default="",
        description="What the workspace, mngr, or the host start said about a failed dispatch, bounded; '' when "
        "none said anything, and for every outcome that is not a failure",
    )

    @property
    def log_description(self) -> str:
        """This dispatch in one log line: the outcome, and its failure detail when there is one."""
        if not self.failure_detail:
            return self.outcome.value
        return f"{self.outcome.value}: {self.failure_detail}"


class UpdateScheduleOutcome(UpperCaseStrEnum):
    """What a call to :meth:`WorkspaceUpdateService.schedule_update` did."""

    ARMED = auto()
    NEEDS_SIGN_IN = auto()
    NO_CRON = auto()
    UNSUPPORTED = auto()
    NOT_RUNNING = auto()
    """The command could not reach the workspace (stopped, or unreachable): a schedule needs it running, through
    the window too."""
    UNREACHABLE = auto()
    """The command gave no answer: cut off in the workspace, or mngr said nothing, so whether it ran is unknown."""
    FAILED = auto()


SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME: Final[dict[UpdateScheduleOutcome, str]] = {
    UpdateScheduleOutcome.NEEDS_SIGN_IN: "Sign in to an AI account in this workspace so it can update itself.",
    UpdateScheduleOutcome.NO_CRON: "This workspace can't run scheduled updates. Update it now instead.",
    UpdateScheduleOutcome.UNSUPPORTED: DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.UNSUPPORTED],
    UpdateScheduleOutcome.NOT_RUNNING: (
        "Couldn't reach this workspace. It has to be running to schedule its update, and during the update window too."
    ),
    UpdateScheduleOutcome.UNREACHABLE: "Couldn't confirm the update was scheduled in this workspace.",
    UpdateScheduleOutcome.FAILED: "Couldn't schedule the update in this workspace.",
}


class UpdateSchedule(FrozenModel):
    """What a schedule (or cancel) did, and the workspace's own words when it did not."""

    outcome: UpdateScheduleOutcome = Field(description="What the arm did")
    failure_detail: str = Field(default="", description="The workspace's or mngr's words about a refusal, bounded")


class UpdateSettingsSource(MutableModel):
    """The app's current update values, as each command carries them into a workspace."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    read_ceiling: Callable[[], str] = Field(frozen=True, description="The app's template ceiling")
    read_update_window: Callable[[], tuple[int, int]] = Field(frozen=True, description="The configured window")
    read_zone: Callable[[], str] = Field(frozen=True, description="The app host's IANA time zone, '' if unknown")
    paths: InstallationPaths | None = Field(
        frozen=True, description="Workspace data paths; None when backups are not configurable at all"
    )

    def is_backup_configured(self, agent_id: AgentId) -> bool:
        """Whether this workspace has a canonical restic env, i.e. backups to fall back on.

        A presence check only: this rides every update frame, and the env's contents are a password.
        """
        return self.paths is not None and has_canonical_env(self.paths, agent_id)

    def settings_for(self, agent_id: AgentId) -> UpdateWorkspaceSettings:
        window = self.read_update_window()
        return UpdateWorkspaceSettings(
            ceiling_ref=self.read_ceiling(),
            window_start_hour=window[0],
            window_end_hour=window[1],
            zone=self.read_zone(),
            is_backup_configured=self.is_backup_configured(agent_id),
        )


def read_workspace_update_facts(
    mngr_caller: MngrCaller, workspace_address: str, settings: UpdateWorkspaceSettings, *, is_sign_in_checked: bool
) -> UpdateWorkspaceFacts | None:
    """The detection sweep's one read of a workspace's updates; None when it did not answer."""
    call = read_update_facts(mngr_caller, workspace_address, settings, is_sign_in_checked=is_sign_in_checked)
    if call.reach is not UpdateCommandReach.ANSWERED or call.payload is None:
        return None
    return parse_update_workspace_facts(call.payload)


def _is_earlier_runs_record(status: UpdateRunStatus, run_started_at: datetime | None) -> bool:
    """Whether ``status``, under the in-flight run's chat name, started before that run was claimed."""
    if status.started_at is None or run_started_at is None:
        return False
    return status.started_at < run_started_at - _EARLIER_RUN_RECORD_MARGIN


def _command_result(call: UpdateCommandCall) -> UpdateCommandResult | None:
    if call.reach is not UpdateCommandReach.ANSWERED or call.payload is None:
        return None
    return parse_update_command_result(call.payload)


class OnUpdateLandedCallback(Protocol):
    """Told when a run's verdict says an update landed: when the run started (None if unreadable) and when it ended."""

    def __call__(self, agent_id: AgentId, *, run_started_at: datetime | None, ended_at: datetime) -> None: ...


class WorkspaceUpdateService(MutableModel):
    """Starts, schedules, and closes out updates; the routes' and the sweep's shared engine."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    state_store: WorkspaceUpdateStateStore = Field(frozen=True, description="Detection, run, and schedule facts.")
    detector: WorkspaceUpdateDetector = Field(frozen=True, description="Re-read a version after a run.")
    apply_window: UpdateApplyWindowManager = Field(frozen=True, description="Probes whether a run is still alive.")
    mngr_caller: MngrCaller = Field(frozen=True, description="Runs every in-workspace command.")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Places each workspace's host.")
    settings_source: UpdateSettingsSource = Field(frozen=True, description="The app's current update values.")
    minds_config: MindsConfig | None = Field(
        frozen=True, description="Where a skipped release is recorded; None in a build with no settings storage"
    )
    start_workspace: Callable[[AgentId], MindHostActionOutcome] = Field(
        frozen=True,
        description=(
            "Brings a workspace's host up when the command could not reach it, returning whether it is "
            "up and why not. The shared host lifecycle action, not a bare ``mngr start``: an update that wakes "
            "a machine the app had stopped must clear its unattended-recovery suppression, or the machine stays "
            "unrecoverable for the rest of the session. The machine stays up afterwards."
        ),
    )
    # agent_id_str -> consecutive WAITING polls; only the poll thread touches it.
    _waiting_streak_by_agent: dict[str, int] = PrivateAttr(default_factory=dict)
    _poll_stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _is_polling_started: bool = PrivateAttr(default=False)
    _on_update_landed_callbacks: list[OnUpdateLandedCallback] = PrivateAttr(default_factory=list)
    _callbacks_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def add_on_update_landed_callback(self, callback: OnUpdateLandedCallback) -> None:
        """Register a callback told each run whose verdict says it landed an update.

        Not told of a run that landed nothing, nor of one that stalls without a verdict.
        """
        with self._callbacks_lock:
            self._on_update_landed_callbacks.append(callback)

    def is_backup_configured(self, agent_id: AgentId) -> bool:
        return self.settings_source.is_backup_configured(agent_id)

    def _address(self, agent_id: AgentId) -> str:
        return build_agent_address(agent_id, self.backend_resolver)

    def dispatch_update(self, agent_id: AgentId, *, target_override: str | None = None) -> UpdateDispatch:
        """Start an update run in ``agent_id`` now, returning what happened.

        ``target_override`` was confirmed at the app's version field when it was chosen. The run
        slot is claimed before any remote work, so a concurrent dispatch loses rather than starting
        a second update. Every non-dispatch exit releases it in ``finally``: a row left on STARTING
        is never polled, so nothing but an app restart could unlock it. The release records why the
        run never went out, on the row every surface reads: the caller that pressed the button may
        be gone by the time the answer comes back.
        """
        if not self.state_store.try_begin_run(agent_id, target_override=target_override or ""):
            return UpdateDispatch(outcome=UpdateDispatchOutcome.ALREADY_RUNNING)
        dispatch: UpdateDispatch | None = None
        try:
            dispatch = self._start_claimed_run(agent_id, target_override or "")
            return dispatch
        finally:
            match dispatch:
                case None:
                    self.state_store.release_failed_claim(agent_id, _DISPATCH_RAISED_FAILURE)
                case UpdateDispatch(outcome=UpdateDispatchOutcome.DISPATCHED):
                    pass
                case UpdateDispatch(outcome=UpdateDispatchOutcome.NEEDS_SIGN_IN):
                    # The row's sign-in state is the answer; a failure line beside it would say it twice.
                    self.state_store.release_failed_claim(agent_id, None)
                case UpdateDispatch(outcome=outcome, failure_detail=failure_detail):
                    self.state_store.release_failed_claim(
                        agent_id,
                        UpdateDispatchFailure(
                            message=DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[outcome], detail=failure_detail
                        ),
                    )

    def _start_claimed_run(self, agent_id: AgentId, override_ref: str) -> UpdateDispatch:
        """Do the work of a dispatch that has already won the run slot.

        The command is tried against the host as it stands, and the host is started only when
        the command could not reach it: a start against a running host costs as much as the
        command itself. The command's own reach is the evidence, not discovery's possibly-stale
        host state.
        """
        settings = self.settings_source.settings_for(agent_id)
        call = start_update_now(self.mngr_caller, self._address(agent_id), settings, override_ref)
        if call.reach is UpdateCommandReach.NOT_REACHED:
            start = self.start_workspace(agent_id)
            if not start.is_successful:
                return UpdateDispatch(
                    outcome=UpdateDispatchOutcome.START_FAILED,
                    # Led by its verdict, so the head is the part worth keeping.
                    failure_detail=(start.failure_reason or "")[:FAILURE_DETAIL_MAX_CHARS],
                )
            call = start_update_now(self.mngr_caller, self._address(agent_id), settings, override_ref)
        match call.reach:
            case UpdateCommandReach.ANSWERED:
                result = _command_result(call)
                assert result is not None
                return self._dispatch_for_result(agent_id, result)
            case UpdateCommandReach.NOT_REACHED:
                return UpdateDispatch(outcome=UpdateDispatchOutcome.UNREACHABLE, failure_detail=call.failure_detail)
            case UpdateCommandReach.CUT_OFF | UpdateCommandReach.NO_ANSWER:
                return UpdateDispatch(outcome=UpdateDispatchOutcome.UNCONFIRMED, failure_detail=call.failure_detail)
            case _ as unreachable:
                assert_never(unreachable)

    def _dispatch_for_result(self, agent_id: AgentId, result: UpdateCommandResult) -> UpdateDispatch:
        match result.outcome:
            case UpdateArmOutcome.CREATED:
                self.state_store.adopt_dispatched_chat(
                    agent_id, chat_agent_name=result.chat_agent_name, chat_agent_id=result.chat_id
                )
                return UpdateDispatch(outcome=UpdateDispatchOutcome.DISPATCHED)
            case UpdateArmOutcome.ALREADY_RUNNING:
                # The run under way is the answer to the press: the row reports it from here.
                if result.chat_agent_name:
                    self.state_store.adopt_run_under_way(
                        agent_id,
                        chat_agent_name=result.chat_agent_name,
                        chat_agent_id=result.chat_id,
                        run_started_at=result.run_started_at,
                    )
                return UpdateDispatch(outcome=UpdateDispatchOutcome.ALREADY_RUNNING, failure_detail=result.detail)
            case UpdateArmOutcome.NEEDS_SIGN_IN:
                self.state_store.record_sign_in_refusal(agent_id, is_refused=True)
                return UpdateDispatch(outcome=UpdateDispatchOutcome.NEEDS_SIGN_IN)
            case UpdateArmOutcome.TOO_OLD_TO_UPDATE:
                return UpdateDispatch(outcome=UpdateDispatchOutcome.UNSUPPORTED)
            case (
                UpdateArmOutcome.CHAT_APP_UNREACHABLE
                | UpdateArmOutcome.CREATE_FAILED
                | UpdateArmOutcome.ARMED
                | UpdateArmOutcome.NO_CRON
                | None
            ):
                return UpdateDispatch(outcome=UpdateDispatchOutcome.SPAWN_FAILED, failure_detail=result.detail)
            case _ as unreachable:
                assert_never(unreachable)

    def schedule_update(self, agent_id: AgentId, *, override_ref: str) -> UpdateSchedule:
        """Arm ``agent_id``'s update for its next window, through the workspace's own schedule file.

        Never starts a stopped workspace: the dispatcher that starts the run lives in the
        workspace's cron, so the workspace has to be running during the window anyway.
        """
        call = arm_update(
            self.mngr_caller, self._address(agent_id), self.settings_source.settings_for(agent_id), override_ref
        )
        result = _command_result(call)
        if result is None:
            outcome = (
                UpdateScheduleOutcome.NOT_RUNNING
                if call.reach is UpdateCommandReach.NOT_REACHED
                else UpdateScheduleOutcome.UNREACHABLE
            )
            return UpdateSchedule(outcome=outcome, failure_detail=call.failure_detail)
        match result.outcome:
            case UpdateArmOutcome.ARMED:
                if result.schedule is not None:
                    self.state_store.record_armed_schedule(agent_id, result.schedule)
                else:
                    logger.warning("The arm of {} answered ARMED with no schedule file to show", agent_id)
                self.detector.request_pass()
                return UpdateSchedule(outcome=UpdateScheduleOutcome.ARMED)
            case UpdateArmOutcome.NEEDS_SIGN_IN:
                self.state_store.record_sign_in_refusal(agent_id, is_refused=True)
                return UpdateSchedule(outcome=UpdateScheduleOutcome.NEEDS_SIGN_IN, failure_detail=result.detail)
            case UpdateArmOutcome.NO_CRON:
                return UpdateSchedule(outcome=UpdateScheduleOutcome.NO_CRON, failure_detail=result.detail)
            case UpdateArmOutcome.TOO_OLD_TO_UPDATE:
                return UpdateSchedule(outcome=UpdateScheduleOutcome.UNSUPPORTED, failure_detail=result.detail)
            case (
                UpdateArmOutcome.CREATED
                | UpdateArmOutcome.ALREADY_RUNNING
                | UpdateArmOutcome.CHAT_APP_UNREACHABLE
                | UpdateArmOutcome.CREATE_FAILED
                | None
            ):
                return UpdateSchedule(outcome=UpdateScheduleOutcome.FAILED, failure_detail=result.detail)
            case _ as unreachable:
                assert_never(unreachable)

    def schedule_update_unwatched(self, agent_id: AgentId, *, override_ref: str) -> UpdateSchedule:
        """Arm ``agent_id``'s update for a caller nobody is waiting on, recording a refusal on the row.

        The sweep's auto-update arm and the bulk schedule both answer before the arm runs, so the
        row is the only place a refusal can reach the user. A sign-in refusal is the row's own state.
        """
        schedule = self.schedule_update(agent_id, override_ref=override_ref)
        if schedule.outcome in (UpdateScheduleOutcome.ARMED, UpdateScheduleOutcome.NEEDS_SIGN_IN):
            return schedule
        logger.info("Could not arm the update of {}: {}", agent_id, schedule.outcome.value)
        self.state_store.record_skipped_dispatch(
            agent_id,
            UpdateDispatchFailure(
                message=SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME[schedule.outcome], detail=schedule.failure_detail
            ),
        )
        return schedule

    def arm_for_auto_update(self, agent_id: AgentId) -> None:
        """The sweep's auto-update arm, at the ceiling.

        Under auto-update the row says nothing of a behind workspace, so one whose arm keeps
        failing would never say so but for the refusal recorded on it.
        """
        self.schedule_update_unwatched(agent_id, override_ref="")

    def cancel_schedule(self, agent_id: AgentId) -> UpdateSchedule | None:
        """Delete the workspace's schedule file; None when it landed, else what stopped it."""
        call = cancel_update_schedule(self.mngr_caller, self._address(agent_id))
        if call.reach is not UpdateCommandReach.ANSWERED:
            outcome = (
                UpdateScheduleOutcome.NOT_RUNNING
                if call.reach is UpdateCommandReach.NOT_REACHED
                else UpdateScheduleOutcome.UNREACHABLE
            )
            return UpdateSchedule(outcome=outcome, failure_detail=call.failure_detail)
        self.state_store.clear_schedule(agent_id)
        # A sweep read that started before the cancel would put the file back on the row.
        self.detector.request_pass()
        return None

    def skip_update(self, agent_id: AgentId) -> UpdateSchedule | None:
        """Skip the current release for this workspace under auto-update: remember it, and cancel its schedule.

        The sweep re-arms it once the app's ceiling moves to a newer release.
        """
        refusal = self.cancel_schedule(agent_id)
        # Only once the cancel landed: a workspace still armed would run the update it was told to skip.
        if refusal is None and self.minds_config is not None:
            self.minds_config.set_skipped_update_target(str(agent_id), self.settings_source.read_ceiling())
        return refusal

    def open_sign_in_chat(self, agent_id: AgentId) -> bool:
        """Open a new chat window in the workspace with the provider chooser up; whether it opened.

        Retried inside the workspace while the user's client connects to it.
        """
        open_window = build_in_workspace_command(
            ["uv", "run", "--no-sync", "workspace-layout", "open", "chat", "--path", _SIGN_IN_CHAT_PATH]
            + ["--if-present", "new"]
        )
        script = (
            f"for attempt in $(seq 1 {_SIGN_IN_OPEN_ATTEMPTS}); do "
            f"if {open_window}; then echo {_SIGN_IN_OPENED_SENTINEL}; exit 0; fi; "
            f"sleep {_SIGN_IN_OPEN_INTERVAL_SECONDS}; done; exit 1"
        )
        result = self.mngr_caller.call(
            ["exec", "--agent", self._address(agent_id), f"bash -c {shlex.quote(script)}", "--no-start"],
            timeout=_SIGN_IN_TIMEOUT_SECONDS,
        )
        is_opened = _SIGN_IN_OPENED_SENTINEL in result.stdout
        if not is_opened:
            logger.warning(
                "Could not open a sign-in chat in {} (exit {}): {}", agent_id, result.returncode, result.stderr.strip()
            )
        return is_opened

    def handle_verdict(
        self,
        agent_id: AgentId,
        verdict: UpdateVerdict,
        resulting_ref: str,
        run_started_at: datetime | None,
        verdict_at: datetime | None,
    ) -> None:
        """Everything the app owes a terminal verdict: re-detect, report, tell the listeners if it landed.

        A record whose verdict time is unreadable is dated at this reading, the
        latest the verdict can have landed.
        """
        # The badge is stale the instant an update lands; drop the cached read and sweep now.
        self.detector.invalidate_cached_version(agent_id)
        self.detector.request_pass()
        if verdict in FAILURE_UPDATE_VERDICTS:
            # Error level so this reaches Sentry; the run's reasoning stays in the user's chat transcript.
            logger.error(
                "Update run in machine {} ended {}: resulting={}",
                agent_id,
                verdict.value,
                resulting_ref or "?",
            )
        elif verdict is UpdateVerdict.NEEDS_DECISION:
            logger.info("Update run in machine {} ended waiting on the user's decision; nothing was applied", agent_id)
        else:
            logger.info(
                "Update run in machine {} ended {}: now on {}",
                agent_id,
                verdict.value,
                resulting_ref or "?",
            )
        if verdict not in LANDED_UPDATE_VERDICTS:
            return
        ended_at = verdict_at if verdict_at is not None else datetime.now(timezone.utc)
        with self._callbacks_lock:
            landed_callbacks = list(self._on_update_landed_callbacks)
        for callback in landed_callbacks:
            callback(agent_id, run_started_at=run_started_at, ended_at=ended_at)

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
            status is not None
            and bool(state.chat_agent_name)
            and status.chat_agent_name == state.chat_agent_name
            and not _is_earlier_runs_record(status, state.run_started_at)
        )
        self.state_store.note_chat_agent_id(agent_id, probe.chat_agent_id)
        if is_records_run:
            assert status is not None
            # The run's own record outranks the claim: its start is what the sweep dedups the file by,
            # its hold is what the modal quotes, and its verdict ends the run.
            self.state_store.adopt_run_record(agent_id, status)
            if status.verdict is not None:
                self._waiting_streak_by_agent.pop(aid_str, None)
                self.apply_window.close_window(agent_id, reason="terminal verdict")
                self.handle_verdict(
                    agent_id, status.verdict, status.resulting_ref, status.started_at, status.verdict_at
                )
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
