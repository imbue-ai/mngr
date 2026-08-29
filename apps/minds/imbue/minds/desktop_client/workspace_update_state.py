"""Whether each workspace is behind the app's template pin, and what is being done about it.

Detection is a positive read: OUT_OF_DATE only when both sides parse as ``minds-v*``
and the workspace sorts below; NEEDS_RECREATION when the workspace's own version
sorts below the hardcoded in-place cutoff, whatever the app is pinned to; anything
else is UNKNOWN, never reported as behind but still dispatchable (the agent reads
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
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.minds_version import parse_minds_version
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleRecord
from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleStore
from imbue.minds.desktop_client.update_status import IN_FLIGHT_ACTIVITIES
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateUnknownReason
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.update_status import describe_skip_reason
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


def derive_update_detection(workspace_ref: str | None, ceiling_ref: str | None) -> UpdateDetection:
    """Classify one workspace against the in-place cutoff and the app's template ceiling.

    The cutoff is checked first and needs only the workspace's side: it is a fact
    about the workspace, not about this build. Past it, both sides must parse as
    ``minds-v*``; a branch ceiling (dev build) makes every workspace UNKNOWN. With
    neither side readable the missing ceiling is reported, since it explains every
    row at once.
    """
    workspace_version = parse_minds_version(workspace_ref)
    ceiling_version = parse_minds_version(ceiling_ref)
    cutoff_version = parse_minds_version(OLDEST_IN_PLACE_UPDATABLE_VERSION)
    assert cutoff_version is not None
    if workspace_version is not None and workspace_version < cutoff_version:
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


# The verdicts that leave the "Updated to X" note on the row.
_SUCCESS_VERDICTS: Final[frozenset[UpdateVerdict]] = frozenset(
    {UpdateVerdict.UPDATED, UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS}
)


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
    # So the sweep's re-read of the run record does not restore a dismissed
    # outcome. In-memory on purpose: a relaunched app re-reports it.
    dismissed_run_started_at: datetime | None = Field(default=None)

    def with_record(self, status: UpdateRunStatus) -> "_RunFacts":
        """These facts with ``status`` as the run's record; a verdict ends the run and may earn the note.

        A record with no readable start keeps the one already held: ``started_at`` is what tells
        this run's record apart from the next's.
        """
        if status.started_at is None:
            status = status.model_copy_update(to_update(status.field_ref().started_at, self.record.started_at))
        if status.verdict is None:
            return self.model_copy_update(to_update(self.field_ref().record, status))
        is_note_earned = status.verdict in _SUCCESS_VERDICTS
        completed_at = status.verdict_at if status.verdict_at is not None else datetime.now(timezone.utc)
        return self.model_copy_update(
            to_update(self.field_ref().activity, UpdateActivity.IDLE),
            to_update(self.field_ref().record, status),
            to_update(
                self.field_ref().success_note_version,
                status.resulting_ref if is_note_earned else self.success_note_version,
            ),
            to_update(self.field_ref().success_note_at, completed_at if is_note_earned else self.success_note_at),
        )


def _compose(detected: _DetectionFacts, run: _RunFacts, schedule: UpdateScheduleRecord | None) -> UiWorkspaceUpdate:
    """One workspace's three slices as the one model every reader sees."""
    is_waiting = run.activity is UpdateActivity.WAITING
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
        scheduled_target_ref=schedule.target_ref if schedule is not None else "",
        last_skip_reason=describe_skip_reason(schedule.last_skip_reason) if schedule is not None else "",
        success_note_version=run.success_note_version,
        chat_agent_name=run.record.chat_agent_name,
    )


OnUpdateStateChangedCallback = Callable[[], None]


class WorkspaceUpdateStateStore(MutableModel):
    """Thread-safe per-workspace update state.

    Detection and run facts are separate slices, and the armed schedule is the schedule
    store's own record, all composed on read: a sweep landing mid-run cannot reset the
    activity, a verdict cannot erase the versions, and the row cannot disagree with the
    intent on disk.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    schedule_store: UpdateScheduleStore = Field(frozen=True, description="The armed intents, read at composition")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _detected_by_agent: dict[str, _DetectionFacts] = PrivateAttr(default_factory=dict)
    _run_by_agent: dict[str, _RunFacts] = PrivateAttr(default_factory=dict)
    _on_change_callbacks: list[OnUpdateStateChangedCallback] = PrivateAttr(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        self.schedule_store.add_on_change_callback(self._fire_on_change)

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

        The armed intent on disk is left alone: absence from a sweep is not evidence
        of destruction (a crashed or slow-to-enumerate provider publishes nothing),
        and the scheduler's own answer to an unreachable machine is to re-arm it.
        """
        aid_str = str(agent_id)
        with self._lock:
            is_present = aid_str in self._detected_by_agent or aid_str in self._run_by_agent
            self._detected_by_agent.pop(aid_str, None)
            self._run_by_agent.pop(aid_str, None)
        if is_present:
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
                to_update(facts.field_ref().activity, activity), to_update(facts.field_ref().record, record)
            )
        self._fire_on_change()
        return True

    def try_begin_run(self, agent_id: AgentId, *, chat_agent_name: str, target_override: str = "") -> bool:
        """Claim the run slot for ``agent_id``, returning whether this caller won it.

        Check and STARTING write share one lock: a dispatch spends minutes before
        it spawns, and three threads dispatch. The claim is the run's start;
        anything the run record reports from before it is an earlier run's.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if facts.activity in IN_FLIGHT_ACTIVITIES:
                return False
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().activity, UpdateActivity.STARTING),
                to_update(
                    facts.field_ref().record,
                    UpdateRunStatus(chat_agent_name=chat_agent_name, started_at=datetime.now(timezone.utc)),
                ),
                to_update(facts.field_ref().target_override, target_override),
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
        with self._lock:
            facts = self._run_by_agent.get(aid_str, _RunFacts())
            if facts.activity in IN_FLIGHT_ACTIVITIES:
                return
            if facts.dismissed_run_started_at is not None and (
                status.started_at is None or status.started_at <= facts.dismissed_run_started_at
            ):
                return
            is_same_run = status.started_at is not None and status.started_at == facts.record.started_at
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
            self._run_by_agent[aid_str] = new_facts
        self._fire_on_change()

    def dismiss_success_note(self, agent_id: AgentId) -> None:
        """Clear the "Updated to X" note for a workspace (the user dismissed it)."""
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str)
            if facts is None or not facts.success_note_version:
                return
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(facts.field_ref().success_note_version, ""),
                to_update(facts.field_ref().success_note_at, None),
            )
        self._fire_on_change()

    def dismiss_run_outcome(self, agent_id: AgentId) -> None:
        """Clear how the last run ended (the user acknowledged it).

        STALLED draws the same "Update failed" badge as a verdict, so it is
        dismissible too and goes back to IDLE. The dismissed run's start is
        remembered so the sweep's re-read of the record does not restore it.
        """
        aid_str = str(agent_id)
        with self._lock:
            facts = self._run_by_agent.get(aid_str)
            if facts is None or (facts.record.verdict is None and facts.activity is not UpdateActivity.STALLED):
                return
            record = facts.record
            self._run_by_agent[aid_str] = facts.model_copy_update(
                to_update(
                    facts.field_ref().activity,
                    UpdateActivity.IDLE if facts.activity is UpdateActivity.STALLED else facts.activity,
                ),
                to_update(
                    facts.field_ref().record,
                    record.model_copy_update(
                        to_update(record.field_ref().verdict, None),
                        to_update(record.field_ref().detail, ""),
                        to_update(record.field_ref().in_place_compatible_ref, ""),
                    ),
                ),
                to_update(
                    facts.field_ref().dismissed_run_started_at,
                    record.started_at if record.started_at is not None else facts.dismissed_run_started_at,
                ),
            )
        self._fire_on_change()

    def get(self, agent_id: AgentId) -> UiWorkspaceUpdate:
        """The composed state for one workspace (all-unknown defaults when unseen)."""
        aid_str = str(agent_id)
        schedule = self.schedule_store.read(agent_id)
        with self._lock:
            detected = self._detected_by_agent.get(aid_str, _DetectionFacts())
            run = self._run_by_agent.get(aid_str, _RunFacts())
        return _compose(detected, run, schedule)

    def snapshot(self) -> dict[str, UiWorkspaceUpdate]:
        """The composed state for every workspace any writer has touched or armed."""
        schedule_by_agent = {record.agent_id: record for record in self.schedule_store.list_records()}
        with self._lock:
            agent_ids = set(self._detected_by_agent) | set(self._run_by_agent) | set(schedule_by_agent)
            return {
                aid_str: _compose(
                    self._detected_by_agent.get(aid_str, _DetectionFacts()),
                    self._run_by_agent.get(aid_str, _RunFacts()),
                    schedule_by_agent.get(aid_str),
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


class WorkspaceUpdateDetector(MutableModel):
    """Background sweep that keeps every workspace's detection slice current.

    One pass per interval plus on-demand passes; an unreachable workspace falls back to its create-time label.
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
    read_run_record: Callable[[AgentId], UpdateRunStatus | None] = Field(
        frozen=True,
        description="Reads the workspace's own run record (``run.json``), injected from the apply-window "
        "probe so this module does not import it. How a run the app did not dispatch -- launched by "
        "hand, or finished while the app was closed -- reaches the row.",
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
        # The run record rides the sweep: it is how an undispatched run, or a
        # verdict written while the app was closed, reaches the row. In-flight
        # rows are the poll's; an unreadable host cannot answer ``--no-start``.
        if not is_workspace_readable(host_state):
            return
        if self.store.get(agent_id).is_run_in_flight:
            return
        status = self.read_run_record(agent_id)
        if status is not None:
            self.store.observe_run_record(agent_id, status)

    def _read_git_version(self, agent_id: AgentId, host_state: HostState | None) -> str | None:
        """The workspace's own ``minds-v*`` tag, from the cache or a fresh exec.

        Only a successful read is cached: an empty read may be transient, and
        caching it would pin a machine that just came up at "unknown" for a whole
        interval. ``read_workspace_git_version`` would answer too, but runs a
        further exec for the upgrade-merge history that detection has no use for.
        """
        aid_str = str(agent_id)
        if not is_workspace_readable(host_state):
            # A read from when the machine was up says nothing about one that has since stopped.
            with self._cache_lock:
                self._cached_read_by_agent.pop(aid_str, None)
            return None
        with self._cache_lock:
            cached = self._cached_read_by_agent.get(aid_str)
            if cached is not None and time.monotonic() - cached.read_at_monotonic < self.interval_seconds:
                return cached.version_ref
        version = read_workspace_current_version(agent_id=agent_id, mngr_caller=self.mngr_caller)
        if version is not None:
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
