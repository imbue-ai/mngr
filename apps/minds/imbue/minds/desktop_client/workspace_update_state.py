"""Whether each workspace is behind the app's template pin, and what is being done about it.

Detection is a positive read: OUT_OF_DATE only when both sides parse as ``minds-v*``
and the workspace is on an earlier minor release; PATCH_AVAILABLE when it sorts below
within the same minor release, which is never prompted for; NEEDS_RECREATION when the
workspace's own version sorts below the hardcoded in-place cutoff, whatever the app is
pinned to; anything else is UNKNOWN, never reported as behind but still dispatchable (the agent reads
the workspace's own upstream). Git is read before the create-time label because the
label never moves after an update. The git read is an ``mngr exec``, so it only
ever runs in the background sweep, never on a render path.
"""

import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.minds_version import parse_minds_version
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.update_dismissal_store import UpdateDismissalStore
from imbue.minds.desktop_client.update_dismissal_store import is_run_dismissed
from imbue.minds.desktop_client.update_status import FAILURE_UPDATE_VERDICTS
from imbue.minds.desktop_client.update_status import IN_FLIGHT_ACTIVITIES
from imbue.minds.desktop_client.update_status import LANDED_UPDATE_VERDICTS
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateDispatchFailure
from imbue.minds.desktop_client.update_status import UpdateLastDispatch
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateScheduleRecord
from imbue.minds.desktop_client.update_status import UpdateSkipReason
from imbue.minds.desktop_client.update_status import UpdateStartedBy
from imbue.minds.desktop_client.update_status import UpdateUnknownReason
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.update_status import describe_skip_reason
from imbue.minds.desktop_client.update_status import format_update_window
from imbue.minds.desktop_client.update_workspace_command import UpdateWorkspaceFacts
from imbue.minds.desktop_client.workspace_version import read_workspace_current_version
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState

# The mngr agent label stamped at create time with the resolved template ref.
ORIGINAL_MINDS_VERSION_LABEL: Final[str] = "original_minds_version"

# The oldest template release an update can still be applied to in place. A
# workspace below it is badged as needing recreation without a run being sent
# in to find that out.
OLDEST_IN_PLACE_UPDATABLE_VERSION: Final[str] = "minds-v0.3.10"


class UpdateDetection(FrozenModel):
    """One workspace's verdict against the app's ceiling, and why it is UNKNOWN when it is."""

    availability: UpdateAvailability = Field(description="The detection verdict")
    unknown_reason: UpdateUnknownReason | None = Field(
        default=None, description="Which side had no version; None whenever the verdict is not UNKNOWN"
    )


def is_below_in_place_update_floor(workspace_ref: str | None) -> bool:
    """Whether a workspace's own version predates everything an in-place update supports.

    A fact about the workspace alone, not about this build, so it is answerable
    without a ceiling -- and it gates every mutation that checks the running
    release's template code out onto the workspace, not only the full update.
    An unreadable version is never below the floor: refusing on a version nobody
    could read would strand ordinary workspaces.
    """
    workspace_version = parse_minds_version(workspace_ref)
    cutoff_version = parse_minds_version(OLDEST_IN_PLACE_UPDATABLE_VERSION)
    assert cutoff_version is not None
    return workspace_version is not None and workspace_version < cutoff_version


def derive_update_detection(workspace_ref: str | None, ceiling_ref: str | None) -> UpdateDetection:
    """Classify one workspace against the in-place cutoff and the app's template ceiling.

    The cutoff is checked first and needs only the workspace's side: it is a fact
    about the workspace, not about this build. Past it, both sides must parse as
    ``minds-v*``; a branch ceiling (dev build) makes every workspace UNKNOWN. With
    neither side readable the missing ceiling is reported, since it explains every
    row at once. Only a newer minor release is prompted for: a workspace behind by
    patch or prerelease alone is PATCH_AVAILABLE.
    """
    workspace_version = parse_minds_version(workspace_ref)
    ceiling_version = parse_minds_version(ceiling_ref)
    if is_below_in_place_update_floor(workspace_ref):
        return UpdateDetection(availability=UpdateAvailability.NEEDS_RECREATION)
    if ceiling_version is None:
        return UpdateDetection(
            availability=UpdateAvailability.UNKNOWN, unknown_reason=UpdateUnknownReason.NO_APP_VERSION
        )
    if workspace_version is None:
        return UpdateDetection(
            availability=UpdateAvailability.UNKNOWN, unknown_reason=UpdateUnknownReason.NO_MACHINE_VERSION
        )
    if workspace_version < ceiling_version:
        if workspace_version.is_same_minor_release(ceiling_version):
            return UpdateDetection(availability=UpdateAvailability.PATCH_AVAILABLE)
        return UpdateDetection(availability=UpdateAvailability.OUT_OF_DATE)
    if ceiling_version < workspace_version:
        return UpdateDetection(availability=UpdateAvailability.APP_BEHIND)
    return UpdateDetection(availability=UpdateAvailability.UP_TO_DATE)


def is_workspace_readable(host_state: HostState | None) -> bool:
    """Whether an in-workspace ``--no-start`` read can answer for a host in this state.

    Unknown counts as readable: a resolver with no host-state data must not leave every workspace unbadged.
    """
    return host_state is None or host_state is HostState.RUNNING


def topology_signature(host_state_by_agent: Mapping[AgentId, HostState | None]) -> frozenset[tuple[str, bool]]:
    """Everything a detection sweep's conclusions depend on, as one comparable value.

    Keyed on readability rather than raw host state so STOPPING -> STOPPED and a
    host missing from one partial snapshot do not read as a machine that moved.
    """
    return frozenset(
        (str(agent_id), is_workspace_readable(host_state)) for agent_id, host_state in host_state_by_agent.items()
    )


class DetectedVersion(FrozenModel):
    """The version read for one workspace, and which source it came from."""

    version_ref: str = Field(default="", description="The ref read, '' when neither source produced one")
    is_from_label: bool = Field(default=False, description="Whether it came from the create-time label")


def resolve_workspace_version(*, git_version: str | None, label_version: str | None) -> DetectedVersion:
    """Pick the version to compare, git over the create-time label: the label never moves after an update."""
    if git_version:
        return DetectedVersion(version_ref=git_version, is_from_label=False)
    if label_version:
        return DetectedVersion(version_ref=label_version, is_from_label=True)
    return DetectedVersion()


class _DetectionFacts(FrozenModel):
    """The detection-sweep half of a workspace's update state."""

    availability: UpdateAvailability = Field(default=UpdateAvailability.UNKNOWN)
    unknown_reason: UpdateUnknownReason | None = Field(default=None)
    current_version: str = Field(default="")
    supported_version: str = Field(default="")
    is_version_from_label: bool = Field(default=False)


class _RunFacts(FrozenModel):
    """A workspace's run as the app knows it: the run's own record, plus what only the app tracks."""

    activity: UpdateActivity = Field(default=UpdateActivity.IDLE)
    record: UpdateRunStatus = Field(
        default_factory=UpdateRunStatus,
        description="The run's own record (``run.json``), or the dispatch's claim until the poll has read one",
    )
    target_override: str = Field(default="")
    success_note_version: str = Field(default="")
    success_note_at: datetime | None = Field(default=None)
    # The note outlives the record once a newer run starts, so a dismissal must
    # name the run that earned it rather than the one on the record.
    success_note_run_started_at: datetime | None = Field(default=None)
    dispatch_failure: UpdateDispatchFailure | None = Field(
        default=None, description="Why the last run asked for never went out; None once one has, or was dismissed"
    )
    # A run record started after this is a run the failed attempt did not account for.
    dispatch_attempted_at: datetime | None = Field(default=None)
    chat_agent_id: str = Field(
        default="", description="The run's chat's id, for opening it: from the create, or the poll's listing"
    )
    is_sign_in_refused: bool = Field(
        default=False, description="Whether the last arm or start was refused because no account is signed in"
    )

    def with_record(self, status: UpdateRunStatus) -> "_RunFacts":
        """These facts with ``status`` as the run's record; a verdict ends the run and may earn the note.

        A record with no readable start keeps the one already held: ``started_at`` is what tells
        this run's record apart from the next's.
        """
        if status.started_at is None:
            status = status.model_copy_update(to_update(status.field_ref().started_at, self.record.started_at))
        if status.verdict is None:
            return self.model_copy_update(to_update(self.field_ref().record, status))
        is_note_earned = status.verdict in LANDED_UPDATE_VERDICTS
        completed_at = status.verdict_at if status.verdict_at is not None else datetime.now(timezone.utc)
        return self.model_copy_update(
            to_update(self.field_ref().activity, UpdateActivity.IDLE),
            to_update(self.field_ref().record, status),
            to_update(
                self.field_ref().success_note_version,
                status.resulting_ref if is_note_earned else self.success_note_version,
            ),
            to_update(self.field_ref().success_note_at, completed_at if is_note_earned else self.success_note_at),
            to_update(
                self.field_ref().success_note_run_started_at,
                status.started_at if is_note_earned else self.success_note_run_started_at,
            ),
        )


class _ScheduleFacts(FrozenModel):
    """The schedule half of a workspace's update state: its schedule file and its last dispatch, as last read."""

    record: UpdateScheduleRecord | None = Field(default=None, description="The armed schedule; None when none is")
    last_dispatch: UpdateLastDispatch | None = Field(default=None, description="The update chat last created there")
    is_signed_in: bool | None = Field(
        default=None, description="Whether an account is signed in, when the read asked; None when it did not"
    )

    @property
    def is_sign_in_skip_standing(self) -> bool:
        """Whether the dispatcher's last attempt found no account, and nothing has said one is signed in since."""
        if self.record is None or self.is_signed_in is True:
            return False
        reason = UpdateSkipReason.NEEDS_SIGN_IN.value
        return reason in (self.record.pending_skip_reason, self.record.last_skip_reason)


def _started_by(run: _RunFacts, schedule: _ScheduleFacts) -> UpdateStartedBy | None:
    """How the row's run was started: its record's own word, else the dispatch that created its chat."""
    if run.record.started_by is not None:
        return run.record.started_by
    dispatch = schedule.last_dispatch
    if dispatch is not None and dispatch.chat_agent_name == run.record.chat_agent_name:
        return dispatch.started_by
    return None


def _chat_agent_id(run: _RunFacts, schedule: _ScheduleFacts) -> str:
    if run.chat_agent_id:
        return run.chat_agent_id
    dispatch = schedule.last_dispatch
    if dispatch is not None and dispatch.chat_agent_name == run.record.chat_agent_name:
        return dispatch.chat_id
    return ""


def _without_outcome(record: UpdateRunStatus) -> UpdateRunStatus:
    """``record`` with how its run ended cleared, as a dismissed badge leaves it."""
    return record.model_copy_update(
        to_update(record.field_ref().verdict, None),
        to_update(record.field_ref().detail, ""),
        to_update(record.field_ref().in_place_compatible_ref, ""),
    )


def _compose(detected: _DetectionFacts, run: _RunFacts, schedule_facts: _ScheduleFacts) -> UiWorkspaceUpdate:
    """One workspace's three slices as the one model every reader sees."""
    is_waiting = run.activity is UpdateActivity.WAITING
    schedule = schedule_facts.record
    return UiWorkspaceUpdate(
        availability=detected.availability,
        unknown_reason=detected.unknown_reason,
        current_version=detected.current_version,
        supported_version=detected.supported_version,
        is_version_from_label=detected.is_version_from_label,
        activity=run.activity,
        run_started_at=run.record.started_at,
        is_hold_recorded=is_waiting and run.record.is_holding,
        hold_detail=run.record.hold_detail if is_waiting and run.record.is_holding else "",
        target_override=run.target_override,
        verdict=run.record.verdict,
        verdict_detail=run.record.detail,
        in_place_compatible_ref=run.record.in_place_compatible_ref,
        is_scheduled=schedule is not None,
        override_ref=schedule.override_ref if schedule is not None else "",
        schedule_window=(
            format_update_window((schedule.window_start_hour, schedule.window_end_hour))
            if schedule is not None
            else ""
        ),
        schedule_zone=schedule.zone if schedule is not None else "",
        last_skip_reason=describe_skip_reason(schedule.last_skip_reason) if schedule is not None else "",
        needs_sign_in=run.is_sign_in_refused or schedule_facts.is_sign_in_skip_standing,
        is_decision_pending=run.record.verdict is UpdateVerdict.NEEDS_DECISION,
        started_by=_started_by(run, schedule_facts),
        success_note_version=run.success_note_version,
        chat_agent_name=run.record.chat_agent_name,
        chat_agent_id=_chat_agent_id(run, schedule_facts),
        dispatch_failure=run.dispatch_failure.message if run.dispatch_failure is not None else "",
        dispatch_failure_detail=run.dispatch_failure.detail if run.dispatch_failure is not None else "",
    )


OnUpdateStateChangedCallback = Callable[[], None]


class WorkspaceUpdateStateStore(MutableModel):
    """Thread-safe per-workspace update state.

    Detection, run, and schedule facts are separate slices composed on read: a sweep
    landing mid-run cannot reset the activity, a verdict cannot erase the versions, and the
    schedule is only ever what the workspace's own file last said.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    dismissal_store: UpdateDismissalStore = Field(
        frozen=True, description="What the user dismissed, so a re-read run record does not restore it"
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _detected_by_agent: dict[str, _DetectionFacts] = PrivateAttr(default_factory=dict)
    _run_by_agent: dict[str, _RunFacts] = PrivateAttr(default_factory=dict)
    _schedule_by_agent: dict[str, _ScheduleFacts] = PrivateAttr(default_factory=dict)
    _on_change_callbacks: list[OnUpdateStateChangedCallback] = PrivateAttr(default_factory=list)

    def add_on_change_callback(self, callback: OnUpdateStateChangedCallback) -> None:
        """Register a callback fired whenever any workspace's composed state changes."""
        with self._lock:
            self._on_change_callbacks.append(callback)

    def _fire_on_change(self) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("An update-state change callback failed: {}", e)

    def record_detection(
        self,
        agent_id: AgentId,
        *,
        detection: UpdateDetection,
        current_version: str,
        supported_version: str,
        is_version_from_label: bool,
    ) -> bool:
        """Store one workspace's detection result; return whether it changed."""
        detected = _DetectionFacts(
            availability=detection.availability,
            unknown_reason=detection.unknown_reason,
            current_version=current_version,
            supported_version=supported_version,
            is_version_from_label=is_version_from_label,
        )
        with self._lock:
            is_changed = self._detected_by_agent.get(str(agent_id)) != detected
            self._detected_by_agent[str(agent_id)] = detected
        if is_changed:
            self._fire_on_change()
        return is_changed

    def forget(self, agent_id: AgentId) -> None:
        """Drop the in-memory slices for a workspace the sweep no longer sees.

        The schedule file in the workspace is left alone: absence from a sweep is not
        evidence of destruction, and the row shows the file again once it is readable.
        """
        aid_str = str(agent_id)
        with self._lock:
            is_present = (
                aid_str in self._detected_by_agent
                or aid_str in self._run_by_agent
                or aid_str in self._schedule_by_agent
            )
            self._detected_by_agent.pop(aid_str, None)
            self._run_by_agent.pop(aid_str, None)
            self._schedule_by_agent.pop(aid_str, None)
        if is_present:
            self._fire_on_change()

    def record_schedule(
        self,
        agent_id: AgentId,
        *,
        schedule: UpdateScheduleRecord | None,
        last_dispatch: UpdateLastDispatch | None,
        is_signed_in: bool | None,
    ) -> None:
        """Store what the workspace's schedule file and last dispatch said; None clears a workspace with neither.

        A workspace that could not be read (stopped) is recorded with neither: it has no cron to
        run a schedule, so nothing is shown as scheduled there. A read that found an account
        signed in ends a sign-in refusal the last Schedule or Update now met.
        """
        facts = _ScheduleFacts(record=schedule, last_dispatch=last_dispatch, is_signed_in=is_signed_in)
        aid_str = str(agent_id)
        with self._lock:
            is_changed = self._schedule_by_agent.get(aid_str, _ScheduleFacts()) != facts
            self._schedule_by_agent[aid_str] = facts
            run = self._run_by_agent.get(aid_str)
            if is_signed_in is True and run is not None and run.is_sign_in_refused:
                self._run_by_agent[aid_str] = run.model_copy_update(
                    to_update(run.field_ref().is_sign_in_refused, False)
                )
                is_changed = True
        if is_changed:
            self._fire_on_change()

    def record_armed_schedule(self, agent_id: AgentId, schedule: UpdateScheduleRecord) -> None:
        """An arm landed: the row is scheduled now, ahead of the sweep's next read, and no longer refused."""
        aid_str = str(agent_id)
        with self._lock:
            previous = self._schedule_by_agent.get(aid_str, _ScheduleFacts())
            self._schedule_by_agent[aid_str] = previous.model_copy_update(
                to_update(previous.field_ref().record, schedule),
                to_update(previous.field_ref().is_signed_in, None),
            )
            run = self._run_by_agent.get(aid_str, _RunFacts())
            self._run_by_agent[aid_str] = run.model_copy_update(
                to_update(run.field_ref().is_sign_in_refused, False),
                to_update(run.field_ref().dispatch_failure, None),
            )
        self._fire_on_change()

    def clear_schedule(self, agent_id: AgentId) -> None:
        """A cancel landed: the row is no longer scheduled."""
        aid_str = str(agent_id)
        with self._lock:
            previous = self._schedule_by_agent.get(aid_str)
            if previous is None or previous.record is None:
                return
            self._schedule_by_agent[aid_str] = previous.model_copy_update(to_update(previous.field_ref().record, None))
        self._fire_on_change()

    def record_sign_in_refusal(self, agent_id: AgentId, *, is_refused: bool) -> None:
        """Note whether the last arm or start was refused for want of a signed-in account."""
        aid_str = str(agent_id)
        with self._lock:
            run = self._run_by_agent.get(aid_str, _RunFacts())
            if run.is_sign_in_refused == is_refused:
                return
            self._run_by_agent[aid_str] = run.model_copy_update(
                to_update(run.field_ref().is_sign_in_refused, is_refused)
            )
        self._fire_on_change()

    def set_activity(
        self, agent_id: AgentId, activity: UpdateActivity, *, only_from: frozenset[UpdateActivity] | None = None
    ) -> bool:
        """Move a workspace's run activity; a run starting from IDLE sheds the previous run's outcome.

        ``only_from`` makes the move conditional under the lock, for the poll that
        decides from a reading up to a probe old; the return says whether it was taken.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if only_from is not None and facts.activity not in only_from:
                return False
            is_new_run = activity is not UpdateActivity.IDLE and facts.activity is UpdateActivity.IDLE
            # The badge must not keep saying "failed" over a run under way again.
            record = (
                UpdateRunStatus(chat_agent_name=facts.record.chat_agent_name, started_at=facts.record.started_at)
                if is_new_run
                else facts.record
            )
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().activity, activity),
                to_update(facts.field_ref().record, record),
                to_update(facts.field_ref().dispatch_failure, None if is_new_run else facts.dispatch_failure),
            )
        self._fire_on_change()
        return True

    def try_begin_run(self, agent_id: AgentId, *, target_override: str) -> bool:
        """Claim the run slot for ``agent_id``, returning whether this caller won it.

        Check and STARTING write share one lock: a dispatch spends minutes before
        it spawns, and three threads dispatch. The claim is the run's start;
        anything the run record reports from before it is an earlier run's. The
        chat's name is the workspace's to pick, so the claim names none until
        :meth:`adopt_dispatched_chat`.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if facts.activity in IN_FLIGHT_ACTIVITIES:
                return False
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().activity, UpdateActivity.STARTING),
                to_update(facts.field_ref().record, UpdateRunStatus(started_at=datetime.now(timezone.utc))),
                to_update(facts.field_ref().target_override, target_override),
                to_update(facts.field_ref().dispatch_failure, None),
                to_update(facts.field_ref().chat_agent_id, ""),
            )
        self._fire_on_change()
        return True

    def adopt_dispatched_chat(self, agent_id: AgentId, *, chat_agent_name: str, chat_agent_id: str) -> bool:
        """The claimed run's chat exists in the workspace, or the run already under way there is this row's now.

        Taken from STARTING only; the row goes RUNNING under the chat's name, so the poll
        matches the run's own record to it. Returns whether it was taken.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if facts.activity is not UpdateActivity.STARTING:
                return False
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().activity, UpdateActivity.RUNNING),
                to_update(
                    facts.field_ref().record,
                    facts.record.model_copy_update(
                        to_update(facts.record.field_ref().chat_agent_name, chat_agent_name)
                    ),
                ),
                to_update(facts.field_ref().chat_agent_id, chat_agent_id),
                to_update(facts.field_ref().is_sign_in_refused, False),
            )
        self._fire_on_change()
        return True

    def note_chat_agent_id(self, agent_id: AgentId, chat_agent_id: str) -> None:
        """Remember the in-flight run's chat id, as the poll's listing names it, so its chat can be opened."""
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str)
            if facts is None or not chat_agent_id or facts.chat_agent_id == chat_agent_id:
                return
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().chat_agent_id, chat_agent_id)
            )
        self._fire_on_change()

    def release_failed_claim(self, agent_id: AgentId, failure: UpdateDispatchFailure | None) -> bool:
        """Hand back a run slot whose dispatch never went out, recording why; return whether it was released.

        Taken only from STARTING: a spawn that timed out after creating the chat
        has a live run the poll may already have entered as RUNNING, and writing
        IDLE over it would let a retry start a second update. Such a run is not
        a failure, so nothing is recorded either.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if facts.activity is not UpdateActivity.STARTING:
                return False
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().activity, UpdateActivity.IDLE),
                to_update(facts.field_ref().dispatch_failure, failure),
                to_update(facts.field_ref().dispatch_attempted_at, facts.record.started_at),
            )
        self._fire_on_change()
        return True

    def record_skipped_dispatch(self, agent_id: AgentId, failure: UpdateDispatchFailure) -> bool:
        """Record why a run asked for was declined before it claimed the slot; return whether it was recorded.

        Nothing for a row with a run in flight: that run is the answer to the request.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if facts.activity in IN_FLIGHT_ACTIVITIES:
                return False
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().dispatch_failure, failure),
                to_update(facts.field_ref().dispatch_attempted_at, datetime.now(timezone.utc)),
            )
        self._fire_on_change()
        return True

    def adopt_run_record(self, agent_id: AgentId, status: UpdateRunStatus) -> None:
        """The poll read the in-flight run's own record: it becomes the row's, and its verdict ends the run.

        Nothing for a row that is not in flight; a verdict landing there is the sweep's to observe.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str)
            if facts is None or facts.activity not in IN_FLIGHT_ACTIVITIES:
                return
            new_facts = facts.with_record(status)
            if new_facts == facts:
                return
            self._run_by_agent[aid_str] = new_facts
        self._fire_on_change()

    def observe_run_record(self, agent_id: AgentId, status: UpdateRunStatus) -> None:
        """Fold the workspace's own run record (``run.json``) into an out-of-flight row.

        How a run the app did not dispatch, or one that finished while it was
        closed, becomes visible. In-flight rows are the poll's. A verdict lands
        without the watched-run close-out: the sweep just re-read the version,
        and there is no schedule attempt to close. ``started_at`` dedups re-reads.
        """
        aid_str = str(agent_id)
        # The one directory glob is paid off the lock the composed reads take; the
        # read that decides is under it, since a dismissal writes the store and
        # clears the row together, and an older store beside the cleared row would
        # restore what was just dismissed.
        self.dismissal_store.load()
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if facts.activity in IN_FLIGHT_ACTIVITIES:
                return
            dismissals = self.dismissal_store.read(agent_id)
            is_same_run = status.started_at is not None and status.started_at == facts.record.started_at
            is_outcome_dismissed = is_run_dismissed(dismissals.outcome_run_started_at, status.started_at)
            # A landed run's note is dismissed on its own, so a relaunch still earns it under a dismissed badge.
            if is_outcome_dismissed and (
                is_same_run or status.started_at is None or status.verdict not in LANDED_UPDATE_VERDICTS
            ):
                return
            if status.verdict is None:
                # A verdictless record with no chat name would lock the row for
                # good: the liveness probe could never match its agent.
                if is_same_run or not status.chat_agent_name:
                    return
                new_facts = facts.model_copy_update(
                    to_update(facts.field_ref().activity, UpdateActivity.RUNNING),
                    to_update(facts.field_ref().record, status),
                    to_update(facts.field_ref().target_override, ""),
                )
            else:
                if is_same_run and facts.record.verdict is status.verdict:
                    return
                new_facts = facts.model_copy_update(to_update(facts.field_ref().target_override, "")).with_record(
                    status
                )
                if new_facts.success_note_version and is_run_dismissed(
                    dismissals.note_run_started_at, new_facts.success_note_run_started_at
                ):
                    new_facts = new_facts.model_copy_update(
                        to_update(new_facts.field_ref().success_note_version, ""),
                        to_update(new_facts.field_ref().success_note_at, None),
                        to_update(new_facts.field_ref().success_note_run_started_at, None),
                    )
                if is_outcome_dismissed:
                    new_facts = new_facts.model_copy_update(
                        to_update(new_facts.field_ref().record, _without_outcome(new_facts.record))
                    )
            # An earlier run's record re-read after a failed attempt says nothing about that attempt.
            is_newer_than_failed_attempt = (
                status.started_at is not None
                and facts.dispatch_attempted_at is not None
                and status.started_at >= facts.dispatch_attempted_at
            )
            if is_newer_than_failed_attempt:
                new_facts = new_facts.model_copy_update(to_update(new_facts.field_ref().dispatch_failure, None))
            self._run_by_agent[aid_str] = new_facts
        self._fire_on_change()

    def dismiss_success_note(self, agent_id: AgentId) -> None:
        """Clear the "Updated to X" note for a workspace (the user dismissed it).

        The run that earned the note is persisted, so neither this launch's sweep
        nor a later one re-earns it from the same record. The write shares the
        lock hold: a sweep landing between the memory clear and the write would
        read a store that does not know about the dismissal yet.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str)
            if facts is None or not facts.success_note_version:
                return
            if facts.success_note_run_started_at is not None:
                self.dismissal_store.dismiss_note(agent_id, facts.success_note_run_started_at)
            else:
                logger.warning(
                    "Clearing {}'s update note without remembering it: the run that earned it has no readable "
                    "start to key the dismissal on, so the next launch will show the note again",
                    agent_id,
                )
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().success_note_version, ""),
                to_update(facts.field_ref().success_note_at, None),
                to_update(facts.field_ref().success_note_run_started_at, None),
            )
        self._fire_on_change()

    def dismiss_run_outcome(self, agent_id: AgentId) -> None:
        """Clear how the last run ended, or why the last one asked for never went out (the user acknowledged it).

        STALLED draws the same "Update failed" badge as a verdict, so it is
        dismissible too and goes back to IDLE. The dismissed run's start is
        persisted so the sweep's re-read of the record, in this launch or a
        later one, does not restore it. A failed dispatch is held only in
        memory, so there is nothing of it to persist.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str)
            if facts is None:
                return
            is_run_outcome_showing = facts.record.verdict is not None or facts.activity is UpdateActivity.STALLED
            if not is_run_outcome_showing and facts.dispatch_failure is None:
                return
            record = facts.record
            if is_run_outcome_showing:
                if record.started_at is not None:
                    self.dismissal_store.dismiss_outcome(agent_id, record.started_at)
                else:
                    logger.warning(
                        "Clearing {}'s update outcome without remembering it: the run has no readable start to key "
                        "the dismissal on, so the next launch will report the outcome again",
                        agent_id,
                    )
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(
                    facts.field_ref().activity,
                    UpdateActivity.IDLE if facts.activity is UpdateActivity.STALLED else facts.activity,
                ),
                to_update(facts.field_ref().record, _without_outcome(record) if is_run_outcome_showing else record),
                to_update(facts.field_ref().dispatch_failure, None),
            )
        self._fire_on_change()

    def get(self, agent_id: AgentId) -> UiWorkspaceUpdate:
        """The composed state for one workspace (all-unknown defaults when unseen)."""
        aid_str = str(agent_id)
        with self._lock:
            detected = self._detected_by_agent.get(aid_str, _DetectionFacts())
            run = self._run_by_agent.get(aid_str, _RunFacts())
            schedule = self._schedule_by_agent.get(aid_str, _ScheduleFacts())
        return _compose(detected, run, schedule)

    def snapshot(self) -> dict[str, UiWorkspaceUpdate]:
        """The composed state for every workspace any writer has touched."""
        with self._lock:
            agent_ids = set(self._detected_by_agent) | set(self._run_by_agent) | set(self._schedule_by_agent)
            return {
                aid_str: _compose(
                    self._detected_by_agent.get(aid_str, _DetectionFacts()),
                    self._run_by_agent.get(aid_str, _RunFacts()),
                    self._schedule_by_agent.get(aid_str, _ScheduleFacts()),
                )
                for aid_str in agent_ids
            }


# A slow backstop: versions change only when an update lands (which fires its
# own refresh) or a workspace starts (a topology change).
_DETECTION_INTERVAL_SECONDS: Final[float] = 300.0

# Bounds how long a quit waits for this strand.
_SHUTDOWN_CHECK_INTERVAL_SECONDS: Final[float] = 1.0

# Each read is an ``mngr exec`` that may spawn its own warm process, so this
# bounds processes rather than just threads.
_MAX_CONCURRENT_VERSION_READS: Final[int] = 4


class _CachedVersionRead(FrozenModel):
    """One successful in-workspace git read, and when it landed."""

    version_ref: str = Field(description="The ``minds-v*`` tag that was read")
    read_at_monotonic: float = Field(description="``time.monotonic()`` when the read landed")
    is_reread_due: bool = Field(
        default=False,
        description="Set once the machine has been unreadable since the read; the next readable sweep re-reads "
        "whatever the interval says",
    )


class AutoUpdatePolicy(FrozenModel):
    """What the app-wide auto-update setting says about one workspace."""

    is_enabled: bool = Field(description="Whether the app arms every behind workspace's update itself")
    skipped_target_ref: str = Field(
        description="The release the user skipped for this workspace; '' when they skipped none"
    )


def should_auto_arm(
    state: UiWorkspaceUpdate, policy: AutoUpdatePolicy, ceiling_ref: str, last_dispatch: UpdateLastDispatch | None
) -> bool:
    """Whether the sweep arms this workspace's update for the user.

    Only a workspace positively read a minor release behind, with nothing armed or running,
    no decision pending, and this release not skipped. A run that failed after its lead's
    best effort, or stalled without a verdict, is not re-armed for the same release: a retry
    needs something to have changed -- a newer ceiling, or the user dismissing the failure.
    """
    if not policy.is_enabled or not state.is_update_offered or state.is_scheduled or state.is_run_in_flight:
        return False
    if state.is_decision_pending or policy.skipped_target_ref == ceiling_ref:
        return False
    if state.verdict in FAILURE_UPDATE_VERDICTS or state.activity is UpdateActivity.STALLED:
        is_failure_for_an_older_ceiling = (
            last_dispatch is not None
            and last_dispatch.chat_agent_name == state.chat_agent_name
            and bool(last_dispatch.ceiling_ref)
            and last_dispatch.ceiling_ref != ceiling_ref
        )
        return is_failure_for_an_older_ceiling
    return True


class WorkspaceUpdateDetector(MutableModel):
    """Background sweep that keeps every workspace's detection slice current.

    One pass per interval plus on-demand passes; a workspace never read this session falls back to its create-time label.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    store: WorkspaceUpdateStateStore = Field(frozen=True, description="Where detection results are published")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Source of workspaces and labels")
    mngr_caller: MngrCaller = Field(frozen=True, description="Runs the in-workspace git read")
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Parent group for the sweep strand and for each sweep's concurrent reads"
    )
    read_supported_version: Callable[[], str] = Field(
        frozen=True, description="The app's template ceiling (``default_workspace_template_ref``)"
    )
    read_update_facts: Callable[[AgentId, bool], UpdateWorkspaceFacts | None] = Field(
        frozen=True,
        description="Runs the workspace's update command in its read mode: the run record, the schedule file "
        "(refreshed on the way with the app's current ceiling, window and zone), and the last dispatch, plus "
        "whether an account is signed in when the second argument asks. How a run the app did not dispatch -- "
        "scheduled, launched by hand, or finished while the app was closed -- reaches the row. None when the "
        "workspace did not answer.",
    )
    read_auto_update_policy: Callable[[AgentId], AutoUpdatePolicy] = Field(
        frozen=True, description="The app-wide auto-update setting and this workspace's skipped release"
    )
    arm_for_auto_update: Callable[[AgentId], None] = Field(
        frozen=True, description="Arms a workspace's update through the update service, as the user's Schedule does"
    )
    interval_seconds: float = Field(default=_DETECTION_INTERVAL_SECONDS, description="Seconds between backstop sweeps")

    _wake_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _cache_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _cached_read_by_agent: dict[str, _CachedVersionRead] = PrivateAttr(default_factory=dict)
    _signature_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _last_topology_signature: frozenset[tuple[str, bool]] | None = PrivateAttr(default=None)
    _is_started: bool = PrivateAttr(default=False)

    def start(self) -> None:
        """Start the sweep strand. Idempotent."""
        if self._is_started:
            return
        self._is_started = True
        self.concurrency_group.start_new_thread(
            target=self._run_loop,
            name="workspace-update-detector",
            daemon=True,
            # The loop logs its own failures; a crash must not poison the root group.
            is_checked=False,
        )

    def stop(self) -> None:
        """Stop the sweep strand (it exits at its next wake)."""
        self._stop_event.set()
        self._wake_event.set()

    def request_pass(self) -> None:
        """Ask for a sweep soon (a verdict landed, a workspace started). Thread-safe."""
        self._wake_event.set()

    def notify_topology_changed(self) -> None:
        """Wake a sweep only if the workspace set or a host's readability moved.

        The resolver's callback fires on every snapshot, and waking on each turned
        the backstop into a continuous ``mngr exec`` loop. Runs on the discovery thread.
        """
        signature = topology_signature(self.backend_resolver.list_active_workspace_host_states())
        with self._signature_lock:
            if signature == self._last_topology_signature:
                return
            self._last_topology_signature = signature
        self._wake_event.set()

    def invalidate_cached_version(self, agent_id: AgentId) -> None:
        """Forget the cached read for a workspace whose version may have just moved."""
        with self._cache_lock:
            self._cached_read_by_agent.pop(str(agent_id), None)

    def run_pass(self) -> None:
        """One synchronous detection sweep over every active workspace."""
        supported_version = self.read_supported_version()
        # Active, not known: the known set retains workspaces whose host was destroyed.
        host_state_by_agent = self.backend_resolver.list_active_workspace_host_states()
        self._detect_all(host_state_by_agent, supported_version)
        active_id_strs = {str(agent_id) for agent_id in host_state_by_agent}
        for stale_id_str, state in self.store.snapshot().items():
            if stale_id_str in active_id_strs:
                continue
            # A crashed provider publishes an empty agent list; forgetting a run
            # on that would unlock the row mid-update and blind the apply window's
            # recovery guard. Only positive evidence ends a run.
            if state.is_run_in_flight:
                continue
            self.store.forget(AgentId(stale_id_str))
        with self._cache_lock:
            for stale_id_str in set(self._cached_read_by_agent) - active_id_strs:
                del self._cached_read_by_agent[stale_id_str]

    def _detect_all(self, host_state_by_agent: Mapping[AgentId, HostState | None], supported_version: str) -> None:
        """Detect every workspace, reading concurrently so no badge waits on discovery's unstable ordering.

        A read that raises ends the pass once every read has returned; the loop
        retries at the next wake.
        """
        items = tuple(host_state_by_agent.items())
        if len(items) <= 1:
            for agent_id, host_state in items:
                self._detect_one(agent_id, host_state, supported_version)
            return
        read_slots = threading.Semaphore(_MAX_CONCURRENT_VERSION_READS)
        # No exit timeout: the group's default is shorter than a real sweep, and
        # a timed-out exit raises ConcurrencyGroupError, which would kill the
        # strand. The reads' own exec timeouts bound the wait.
        with self.concurrency_group.make_concurrency_group(
            "workspace-update-reads", exit_timeout_seconds=float("inf")
        ) as reads:
            for agent_id, host_state in items:
                reads.start_new_thread(
                    target=self._detect_one_within_slot,
                    args=(read_slots, agent_id, host_state, supported_version),
                    name=f"workspace-update-read-{agent_id}",
                    daemon=True,
                )

    def _detect_one_within_slot(
        self,
        read_slots: threading.Semaphore,
        agent_id: AgentId,
        host_state: HostState | None,
        supported_version: str,
    ) -> None:
        """One concurrent read, held to the sweep's slot count."""
        with read_slots:
            self._detect_one(agent_id, host_state, supported_version)

    def _detect_one(self, agent_id: AgentId, host_state: HostState | None, supported_version: str) -> None:
        label_version = self.backend_resolver.get_agent_label(agent_id, ORIGINAL_MINDS_VERSION_LABEL)
        git_version = self._read_git_version(agent_id, host_state)
        detected = resolve_workspace_version(git_version=git_version, label_version=label_version)
        self.store.record_detection(
            agent_id,
            detection=derive_update_detection(detected.version_ref or None, supported_version or None),
            current_version=detected.version_ref,
            supported_version=supported_version,
            is_version_from_label=detected.is_from_label,
        )
        # The run record and the schedule file ride the sweep: they are how a
        # scheduled or undispatched run, or a verdict written while the app was
        # closed, reaches the row. An unreadable host cannot answer ``--no-start``,
        # and a stopped one has no cron to run a schedule.
        if not is_workspace_readable(host_state):
            self.store.record_schedule(agent_id, schedule=None, last_dispatch=None, is_signed_in=None)
            return
        # A row that says it needs a sign-in asks whether one has landed since, so it can stop saying so.
        facts = self.read_update_facts(agent_id, self.store.get(agent_id).needs_sign_in)
        if facts is None:
            return
        if facts.run_status is not None:
            self.store.observe_run_record(agent_id, facts.run_status)
        self.store.record_schedule(
            agent_id, schedule=facts.schedule, last_dispatch=facts.last_dispatch, is_signed_in=facts.is_signed_in
        )
        if should_auto_arm(
            self.store.get(agent_id), self.read_auto_update_policy(agent_id), supported_version, facts.last_dispatch
        ):
            logger.info("Arming the update of {} under auto-update", agent_id)
            self.arm_for_auto_update(agent_id)

    def _read_git_version(self, agent_id: AgentId, host_state: HostState | None) -> str | None:
        """The workspace's own ``minds-v*`` tag, from the cache or a fresh exec.

        A read this session outranks the create-time label for as long as the
        machine is known: its version moves only when an update lands inside it,
        which invalidates the cache, so neither a stretch of unreadability (a
        stop, a start the app itself announced, a discovery gap) nor a read that
        failed (an exec timeout under slow discovery) is evidence that the last
        read is wrong. The label answers only for a machine never read at all.

        A machine that was unreadable is re-read as soon as it is readable again,
        whatever the cache's age. Only a successful read is cached: an empty read
        may be transient, and caching it would pin a machine that just came up at
        "unknown" for a whole interval. ``read_workspace_git_version`` would
        answer too, but runs a further exec for the upgrade-merge history that
        detection has no use for.
        """
        aid_str = str(agent_id)
        with self._cache_lock:
            cached = self._cached_read_by_agent.get(aid_str)
            if not is_workspace_readable(host_state):
                if cached is not None:
                    self._cached_read_by_agent[aid_str] = cached.model_copy_update(
                        to_update(cached.field_ref().is_reread_due, True)
                    )
                return cached.version_ref if cached is not None else None
            is_fresh = cached is not None and time.monotonic() - cached.read_at_monotonic < self.interval_seconds
            if cached is not None and is_fresh and not cached.is_reread_due:
                return cached.version_ref
        version = read_workspace_current_version(
            agent_address=build_agent_address(agent_id, self.backend_resolver), mngr_caller=self.mngr_caller
        )
        if version is None:
            return cached.version_ref if cached is not None else None
        with self._cache_lock:
            self._cached_read_by_agent[aid_str] = _CachedVersionRead(
                version_ref=version, read_at_monotonic=time.monotonic()
            )
        return version

    def _is_stopping(self) -> bool:
        return self._stop_event.is_set() or self.concurrency_group.is_shutting_down()

    def _wait_for_next_pass(self) -> None:
        """Sleep until the interval elapses, a pass is requested, or we are stopping.

        Sliced because a quit sets the group's event, not the private wake event.
        """
        deadline = time.monotonic() + self.interval_seconds
        while not self._is_stopping():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake_event.wait(timeout=min(_SHUTDOWN_CHECK_INTERVAL_SECONDS, remaining)):
                self._wake_event.clear()
                return

    def _run_loop(self) -> None:
        while not self._is_stopping():
            try:
                self.run_pass()
            # ``except*``: a sweep's reads run as a group, which reports their
            # failures as a ConcurrencyExceptionGroup; this tolerates the same
            # set inside one as it does bare, and re-raises anything else.
            except* (MindError, MngrError, OSError, RuntimeError, ValueError) as e:
                # One unreadable workspace must not freeze the badges; anything
                # outside this set is a bug and kills the strand.
                logger.warning("A workspace-update detection sweep failed; retrying at the next wake: {}", e)
            self._wait_for_next_pass()
        logger.debug("Exited the workspace-update detection loop")
