"""Detection: version comparison, the tri-state, and how the composed state is assembled."""

import json
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.testing import landed_verdict
from imbue.minds.desktop_client.testing import make_update_state_store
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateUnknownReason
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.workspace_update_state import ORIGINAL_MINDS_VERSION_LABEL
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateDetector
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.desktop_client.workspace_update_state import derive_update_detection
from imbue.minds.desktop_client.workspace_update_state import resolve_workspace_version
from imbue.minds.desktop_client.workspace_update_state import topology_signature
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState


@pytest.mark.witnesses("workspace-updates.behind-the-app", partial="the detection verdict only")
def test_a_workspace_below_the_ceiling_is_out_of_date() -> None:
    assert derive_update_detection("minds-v0.3.12", "minds-v0.4.1").availability is UpdateAvailability.OUT_OF_DATE


@pytest.mark.witnesses("workspace-updates.at-the-app", partial="the detection verdict only")
def test_a_workspace_at_the_ceiling_is_up_to_date() -> None:
    assert derive_update_detection("minds-v0.4.1", "minds-v0.4.1").availability is UpdateAvailability.UP_TO_DATE


@pytest.mark.witnesses("workspace-updates.ahead-of-the-app", partial="the detection verdict only")
def test_a_workspace_above_the_ceiling_reports_the_app_as_behind() -> None:
    """Reverse divergence is its own answer: there is nothing to run in the workspace."""
    assert derive_update_detection("minds-v0.5.0", "minds-v0.4.1").availability is UpdateAvailability.APP_BEHIND


@pytest.mark.witnesses("workspace-updates.development-build", partial="the detection verdict only")
def test_a_dev_build_imposes_no_ceiling() -> None:
    """A branch ceiling cannot name a version, so it must not badge anything."""
    assert derive_update_detection("minds-v0.3.10", "main").availability is UpdateAvailability.UNKNOWN


def _state_with(availability: UpdateAvailability) -> UiWorkspaceUpdate:
    return UiWorkspaceUpdate(
        availability=availability,
        current_version="",
        supported_version="",
        is_version_from_label=False,
        activity=UpdateActivity.IDLE,
    )


@pytest.mark.witnesses("workspace-updates.too-old-to-update-in-place", partial="the detection verdict only")
def test_a_workspace_below_the_in_place_cutoff_needs_recreation_whatever_the_app_is_pinned_to() -> None:
    """The cutoff is a fact about the workspace, so a dev build's missing ceiling does not hide it."""
    assert derive_update_detection("minds-v0.3.9", "minds-v0.4.1").availability is UpdateAvailability.NEEDS_RECREATION
    assert derive_update_detection("minds-v0.3.9", "main").availability is UpdateAvailability.NEEDS_RECREATION
    assert derive_update_detection("minds-v0.2.0", None).availability is UpdateAvailability.NEEDS_RECREATION


def test_the_in_place_cutoff_is_inclusive() -> None:
    """A workspace at the cutoff release itself can still be updated in place."""
    assert derive_update_detection("minds-v0.3.10", "minds-v0.4.1").availability is UpdateAvailability.OUT_OF_DATE


def test_a_workspace_that_needs_recreation_is_neither_offered_nor_dispatchable() -> None:
    """Sending the update agent in would only have it discover what the app already read."""
    too_old = _state_with(UpdateAvailability.NEEDS_RECREATION)
    assert too_old.is_update_offered is False
    assert too_old.is_update_dispatchable is False


@pytest.mark.witnesses("workspace-updates.no-workspace-version", partial="the detection verdict only")
def test_a_workspace_with_no_readable_version_is_unknown() -> None:
    """A custom-template or tagless workspace is never reported as out of date."""
    assert derive_update_detection(None, "minds-v0.4.1").availability is UpdateAvailability.UNKNOWN
    assert derive_update_detection("my-own-template", "minds-v0.4.1").availability is UpdateAvailability.UNKNOWN


def test_unknown_is_never_claimed_as_behind_but_is_still_worth_asking() -> None:
    """Saying a machine is behind needs a reading; sending in the agent that can produce one does not."""
    unknown = _state_with(UpdateAvailability.UNKNOWN)
    assert unknown.is_update_offered is False
    assert unknown.is_update_dispatchable is True

    for availability in (UpdateAvailability.UP_TO_DATE, UpdateAvailability.APP_BEHIND):
        settled = _state_with(availability)
        assert settled.is_update_offered is False
        assert settled.is_update_dispatchable is False


@pytest.mark.witnesses("workspace-updates.unknown-names-the-missing-side")
def test_unknown_names_which_side_had_no_version() -> None:
    """A dev build reads unknown over machines whose version it read perfectly well."""
    dev_build = derive_update_detection("minds-v0.3.12", "gabriel/some-branch")
    assert dev_build.unknown_reason is UpdateUnknownReason.NO_APP_VERSION

    custom_template = derive_update_detection("my-own-template", "minds-v0.4.1")
    assert custom_template.unknown_reason is UpdateUnknownReason.NO_MACHINE_VERSION

    # Neither side readable: the missing ceiling wins, since it explains every row.
    assert derive_update_detection(None, "main").unknown_reason is UpdateUnknownReason.NO_APP_VERSION


def test_a_positive_verdict_carries_no_unknown_reason() -> None:
    for workspace_ref in ("minds-v0.3.12", "minds-v0.4.1", "minds-v0.5.0"):
        assert derive_update_detection(workspace_ref, "minds-v0.4.1").unknown_reason is None


@pytest.mark.witnesses("workspace-updates.updated-workspace-not-re-offered")
def test_git_wins_over_the_create_time_label() -> None:
    """A workspace that already updated itself must not be badged off its birth version."""
    detected = resolve_workspace_version(git_version="minds-v0.4.1", label_version="minds-v0.3.1")
    assert detected.version_ref == "minds-v0.4.1"
    assert detected.is_from_label is False


def test_the_label_answers_for_a_workspace_git_cannot_be_read_in() -> None:
    """A stopped workspace still has the one version fact knowable offline."""
    detected = resolve_workspace_version(git_version=None, label_version="minds-v0.3.1")
    assert detected.version_ref == "minds-v0.3.1"
    assert detected.is_from_label is True


def test_a_workspace_with_neither_source_reports_nothing() -> None:
    detected = resolve_workspace_version(git_version=None, label_version=None)
    assert detected.version_ref == ""


def _out_of_date_store(tmp_path: Path) -> tuple[WorkspaceUpdateStateStore, AgentId]:
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()
    store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.OUT_OF_DATE),
        current_version="minds-v0.3.12",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )
    return store, agent_id


def test_a_run_starting_does_not_disturb_the_detected_versions(tmp_path: Path) -> None:
    """The three writers own separate slices; a run must not blank the version display."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)

    state = store.get(agent_id)
    assert state.current_version == "minds-v0.3.12"
    assert state.supported_version == "minds-v0.4.1"
    assert state.activity is UpdateActivity.RUNNING


def test_a_detection_sweep_landing_mid_run_does_not_reset_the_activity(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.APPLYING)

    store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.OUT_OF_DATE),
        current_version="minds-v0.3.12",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )

    assert store.get(agent_id).activity is UpdateActivity.APPLYING


def test_a_new_run_clears_the_previous_run_s_verdict(tmp_path: Path) -> None:
    """The badge must not keep saying "failed" over a run that is going again."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    store.adopt_run_record(
        agent_id, landed_verdict(UpdateVerdict.STUCK, detail="it wedged", resulting_ref="minds-v0.3.12")
    )
    assert store.get(agent_id).verdict is UpdateVerdict.STUCK

    store.set_activity(agent_id, UpdateActivity.STARTING)

    assert store.get(agent_id).verdict is None


@pytest.mark.witnesses("workspace-updates.updated-note", partial="earning the note only")
def test_a_run_that_landed_earns_the_updated_note(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    store.adopt_run_record(agent_id, landed_verdict(UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1"))

    assert store.get(agent_id).success_note_version == "minds-v0.4.1"


def test_dismissing_the_note_keeps_the_rest_of_the_run_s_story(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    store.adopt_run_record(
        agent_id,
        landed_verdict(
            UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS,
            detail="Your dashboard needs a look.",
            resulting_ref="minds-v0.4.1",
        ),
    )

    store.dismiss_success_note(agent_id)

    state = store.get(agent_id)
    assert state.success_note_version == ""
    assert state.verdict is UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS
    assert state.verdict_detail == "Your dashboard needs a look."


def test_a_failed_run_earns_no_note(tmp_path: Path) -> None:
    """There is nothing to congratulate; the verdict is what the row has to say."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    store.adopt_run_record(agent_id, landed_verdict(UpdateVerdict.STUCK, resulting_ref="minds-v0.3.12"))

    state = store.get(agent_id)
    assert state.success_note_version == ""
    assert state.verdict is UpdateVerdict.STUCK


def test_forgetting_a_destroyed_workspace_drops_every_slice(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)

    store.forget(agent_id)

    assert str(agent_id) not in store.snapshot()


def test_forgetting_a_workspace_the_sweep_lost_sight_of_keeps_its_armed_schedule(tmp_path: Path) -> None:
    """A provider that crashed or has not enumerated yet publishes nothing; that must not disarm the user's intent."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.schedule_store.schedule(agent_id, target_ref="minds-v0.5.0")

    store.forget(agent_id)

    assert store.schedule_store.read(agent_id) is not None
    state = store.get(agent_id)
    assert state.is_scheduled is True
    assert state.scheduled_target_ref == "minds-v0.5.0"


def test_dismissing_a_run_outcome_clears_a_stall_as_well_as_a_verdict(tmp_path: Path) -> None:
    """Both draw the row's "Update failed" badge, so both have to be dismissible."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    store.set_activity(agent_id, UpdateActivity.STALLED)

    store.dismiss_run_outcome(agent_id)

    state = store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is None


def test_dismissing_a_run_outcome_leaves_a_live_run_alone(tmp_path: Path) -> None:
    """The dismissal is about a run that has ended; an in-flight one has not."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)

    store.dismiss_run_outcome(agent_id)

    assert store.get(agent_id).activity is UpdateActivity.RUNNING


def test_a_conditional_activity_write_is_taken_only_while_the_row_has_not_moved(tmp_path: Path) -> None:
    """A row that moved on since the reading a write was decided from keeps the later reading."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.set_activity(agent_id, UpdateActivity.RUNNING)

    is_moved = store.set_activity(agent_id, UpdateActivity.WAITING, only_from=frozenset({UpdateActivity.RUNNING}))
    assert is_moved is True
    assert store.get(agent_id).activity is UpdateActivity.WAITING

    store.adopt_run_record(agent_id, landed_verdict(UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1"))
    is_moved = store.set_activity(
        agent_id, UpdateActivity.STALLED, only_from=frozenset({UpdateActivity.RUNNING, UpdateActivity.WAITING})
    )

    assert is_moved is False
    state = store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is UpdateVerdict.UPDATED


def test_a_run_in_flight_is_what_locks_the_row(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    for activity in (UpdateActivity.STARTING, UpdateActivity.RUNNING, UpdateActivity.WAITING, UpdateActivity.APPLYING):
        store.set_activity(agent_id, activity)
        assert store.get(agent_id).is_run_in_flight is True
    for activity in (UpdateActivity.IDLE, UpdateActivity.STALLED):
        store.set_activity(agent_id, activity)
        assert store.get(agent_id).is_run_in_flight is False


# -- The detection sweep: what it reads, what it skips, and what it reuses -----


class _WorkspacesResolver(StaticBackendResolver):
    """A fixed set of active workspaces, their host states, and their create-time labels."""

    host_state_by_agent: Mapping[AgentId, HostState | None] = Field(default_factory=dict)
    label_by_agent: Mapping[str, str] = Field(default_factory=dict)

    def list_active_workspace_host_states(self) -> Mapping[AgentId, HostState | None]:
        return self.host_state_by_agent

    def get_agent_label(self, agent_id: AgentId, label_key: str) -> str | None:
        if label_key != ORIGINAL_MINDS_VERSION_LABEL:
            return None
        return self.label_by_agent.get(str(agent_id))


class _VersionReadingMngrCaller(MngrCaller):
    """Answers the one-exec version read per agent (marker line, then tag), recording who was asked.

    ``barrier`` (when set) is waited on inside every call, so a test can assert the reads overlap.
    """

    version_by_agent: Mapping[str, str] = Field(default_factory=dict)
    marker_subject_by_agent: Mapping[str, str] = Field(default_factory=dict)
    barrier: threading.Barrier | None = Field(default=None)
    _read_agent_ids: list[str] = PrivateAttr(default_factory=list)
    _read_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        agent_id_str = argv[2]
        with self._read_lock:
            self._read_agent_ids.append(agent_id_str)
        if self.barrier is not None:
            self.barrier.wait(timeout=10.0)
        # Neither line is a failure of the command: ``git log`` prints nothing for
        # no marker, and the read discards ``describe``'s exit code.
        lines = [self.marker_subject_by_agent.get(agent_id_str), self.version_by_agent.get(agent_id_str)]
        stdout = "".join(f"{line}\n" for line in lines if line is not None)
        return MngrCallResult(returncode=0, stdout=json.dumps({"results": [{"stdout": stdout}]}))

    @property
    def read_agent_ids(self) -> list[str]:
        with self._read_lock:
            return list(self._read_agent_ids)


def _detector(
    tmp_path: Path,
    resolver: _WorkspacesResolver,
    caller: MngrCaller,
    concurrency_group: ConcurrencyGroup,
    *,
    store: WorkspaceUpdateStateStore | None = None,
    interval_seconds: float = 300.0,
    read_run_record: Callable[[AgentId], UpdateRunStatus | None] | None = None,
) -> WorkspaceUpdateDetector:
    return WorkspaceUpdateDetector(
        store=store if store is not None else make_update_state_store(tmp_path),
        backend_resolver=resolver,
        mngr_caller=caller,
        concurrency_group=concurrency_group,
        read_supported_version=lambda: "minds-v0.4.1",
        interval_seconds=interval_seconds,
        read_run_record=read_run_record if read_run_record is not None else lambda _agent_id: None,
    )


def test_a_stopped_workspace_is_read_from_its_label_without_an_exec(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The read passes --no-start, so a stopped machine cannot answer it."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={agent_id: HostState.STOPPED},
        label_by_agent={str(agent_id): "minds-v0.3.12"},
    )
    caller = _VersionReadingMngrCaller()
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)

    detector.run_pass()

    assert caller.read_agent_ids == []
    state = detector.store.get(agent_id)
    assert state.current_version == "minds-v0.3.12"
    assert state.is_version_from_label is True
    assert state.availability is UpdateAvailability.OUT_OF_DATE


def test_an_unknown_host_state_is_still_read(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    """A resolver with no host-state data must not leave every machine unbadged."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(url_by_agent_and_service={}, host_state_by_agent={agent_id: None})
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.3.12"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)

    detector.run_pass()

    assert caller.read_agent_ids == [str(agent_id)]
    assert detector.store.get(agent_id).current_version == "minds-v0.3.12"


def test_a_tagless_machine_reads_the_update_it_already_applied(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Its clone has no ``minds-v*`` tag and the label is its birth version, so only the marker can name the update."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={agent_id: HostState.RUNNING},
        label_by_agent={str(agent_id): "minds-v0.3.17"},
    )
    caller = _VersionReadingMngrCaller(
        marker_subject_by_agent={str(agent_id): "update-self: merge upstream template (minds-v0.4.1)"},
    )
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)

    detector.run_pass()

    state = detector.store.get(agent_id)
    assert state.current_version == "minds-v0.4.1"
    assert state.is_version_from_label is False
    assert state.availability is UpdateAvailability.UP_TO_DATE


def test_a_machine_with_neither_marker_nor_tag_still_falls_back_to_its_label(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Both reads coming up empty is the one case the create-time label answers."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={agent_id: HostState.RUNNING},
        label_by_agent={str(agent_id): "minds-v0.3.17"},
    )
    detector = _detector(tmp_path, resolver, _VersionReadingMngrCaller(), root_concurrency_group)

    detector.run_pass()

    state = detector.store.get(agent_id)
    assert state.current_version == "minds-v0.3.17"
    assert state.is_version_from_label is True
    assert state.availability is UpdateAvailability.OUT_OF_DATE


def test_a_second_sweep_reuses_the_version_it_already_read(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A version moves when an update lands or a machine starts, and not otherwise."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(url_by_agent_and_service={}, host_state_by_agent={agent_id: HostState.RUNNING})
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.3.12"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)

    detector.run_pass()
    detector.run_pass()
    detector.run_pass()

    assert caller.read_agent_ids == [str(agent_id)]
    assert detector.store.get(agent_id).current_version == "minds-v0.3.12"


def test_a_machine_that_just_started_is_re_read(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    """Its git only became readable now; the label it fell back to is its birth version."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={agent_id: HostState.STOPPED},
        label_by_agent={str(agent_id): "minds-v0.3.1"},
    )
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.4.1"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)
    detector.run_pass()

    resolver.host_state_by_agent = {agent_id: HostState.RUNNING}
    detector.run_pass()

    assert caller.read_agent_ids == [str(agent_id)]
    assert detector.store.get(agent_id).current_version == "minds-v0.4.1"
    assert detector.store.get(agent_id).availability is UpdateAvailability.UP_TO_DATE


def test_a_landed_update_invalidates_the_cached_version(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Otherwise the badge would keep showing the version the update replaced."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(url_by_agent_and_service={}, host_state_by_agent={agent_id: HostState.RUNNING})
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.3.12"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)
    detector.run_pass()

    caller.version_by_agent = {str(agent_id): "minds-v0.4.1"}
    detector.invalidate_cached_version(agent_id)
    detector.run_pass()

    assert caller.read_agent_ids == [str(agent_id), str(agent_id)]
    assert detector.store.get(agent_id).current_version == "minds-v0.4.1"


def test_a_read_that_came_back_empty_is_not_cached(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    """A machine that has just come up must not be pinned to a failed read for a whole interval."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(url_by_agent_and_service={}, host_state_by_agent={agent_id: HostState.RUNNING})
    caller = _VersionReadingMngrCaller(version_by_agent={})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)

    detector.run_pass()
    caller.version_by_agent = {str(agent_id): "minds-v0.3.12"}
    detector.run_pass()

    assert caller.read_agent_ids == [str(agent_id), str(agent_id)]
    assert detector.store.get(agent_id).current_version == "minds-v0.3.12"


def test_the_reads_of_a_sweep_overlap(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    """Serially, a machine's badge waited on every machine ahead of it in discovery's unstable order."""
    agent_ids = [AgentId.generate() for _ in range(3)]
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={agent_id: HostState.RUNNING for agent_id in agent_ids},
    )
    # Nothing is released until all three reads are in flight at once.
    caller = _VersionReadingMngrCaller(
        version_by_agent={str(agent_id): "minds-v0.3.12" for agent_id in agent_ids},
        barrier=threading.Barrier(len(agent_ids)),
    )
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)

    detector.run_pass()

    assert sorted(caller.read_agent_ids) == sorted(str(agent_id) for agent_id in agent_ids)
    for agent_id in agent_ids:
        assert detector.store.get(agent_id).availability is UpdateAvailability.OUT_OF_DATE


def test_a_workspace_that_is_gone_loses_its_cached_read(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A destroyed workspace must not leave its version behind for an id that comes back."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(url_by_agent_and_service={}, host_state_by_agent={agent_id: HostState.RUNNING})
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.3.12"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)
    detector.run_pass()

    resolver.host_state_by_agent = {}
    detector.run_pass()
    resolver.host_state_by_agent = {agent_id: HostState.RUNNING}
    detector.run_pass()

    assert caller.read_agent_ids == [str(agent_id), str(agent_id)]


def test_a_sweep_that_loses_a_workspace_keeps_the_row_whose_run_is_in_flight(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A crashed provider publishes no agents at all; only positive evidence ends a run."""
    updating_id, idle_id = AgentId.generate(), AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={updating_id: HostState.RUNNING, idle_id: HostState.RUNNING},
    )
    caller = _VersionReadingMngrCaller(
        version_by_agent={str(updating_id): "minds-v0.3.12", str(idle_id): "minds-v0.3.12"}
    )
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)
    detector.run_pass()
    detector.store.set_activity(updating_id, UpdateActivity.APPLYING)

    resolver.host_state_by_agent = {}
    detector.run_pass()

    assert detector.store.get(updating_id).activity is UpdateActivity.APPLYING
    assert detector.store.get(idle_id).activity is UpdateActivity.IDLE
    assert str(idle_id) not in detector.store.snapshot()


def test_only_a_material_discovery_change_is_worth_a_sweep() -> None:
    """The resolver fires its change callback on every snapshot, identical or not."""
    agent_id, other_id = AgentId.generate(), AgentId.generate()
    running = {agent_id: HostState.RUNNING}

    assert topology_signature(running) == topology_signature({agent_id: HostState.RUNNING})
    assert topology_signature(running) != topology_signature({agent_id: HostState.STOPPED})
    assert topology_signature(running) != topology_signature({agent_id: HostState.RUNNING, other_id: None})
    assert topology_signature(running) != topology_signature({})
    # Neither a host dropping out of a partial snapshot nor one passing through STOPPING is a move.
    assert topology_signature(running) == topology_signature({agent_id: None})
    assert topology_signature({agent_id: HostState.STOPPING}) == topology_signature({agent_id: HostState.STOPPED})


# -- The workspace's own run record ------------------------------------------


def _run_record(
    *,
    chat: str = "update-abc123",
    started_at: datetime | None = None,
    verdict: UpdateVerdict | None = None,
    resulting_ref: str = "",
) -> UpdateRunStatus:
    return UpdateRunStatus(
        chat_agent_name=chat,
        started_at=started_at if started_at is not None else datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc),
        verdict=verdict,
        resulting_ref=resulting_ref,
        verdict_at=datetime(2026, 8, 26, 3, 20, tzinfo=timezone.utc) if verdict is not None else None,
    )


def test_a_verdictless_record_enters_the_row_as_a_running_run(tmp_path: Path) -> None:
    """How a run the user launched by hand in a chat becomes visible."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()

    store.observe_run_record(agent_id, _run_record())

    state = store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.chat_agent_name == "update-abc123"
    assert state.is_run_in_flight is True


def test_a_record_naming_no_run_is_not_entered_as_a_running_one(tmp_path: Path) -> None:
    """Entering a record the liveness probe cannot match as RUNNING would lock the row for good; its verdict still lands."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()

    store.observe_run_record(agent_id, _run_record(chat=""))

    assert store.get(agent_id).activity is UpdateActivity.IDLE
    assert store.get(agent_id).is_run_in_flight is False

    store.observe_run_record(agent_id, _run_record(chat="", verdict=UpdateVerdict.STUCK))

    assert store.get(agent_id).verdict is UpdateVerdict.STUCK


def test_a_recorded_verdict_lands_on_an_idle_row_with_its_note(tmp_path: Path) -> None:
    """A run that finished while the app was closed still reaches the badge and earns its note."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()

    store.observe_run_record(
        agent_id,
        _run_record(verdict=UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1"),
    )

    state = store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is UpdateVerdict.UPDATED
    assert state.success_note_version == "minds-v0.4.1"


def test_a_record_never_touches_a_row_with_a_run_in_flight(tmp_path: Path) -> None:
    """The liveness poll owns in-flight rows."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()
    assert store.try_begin_run(agent_id, chat_agent_name="update-new")

    store.observe_run_record(agent_id, _run_record(chat="update-old", verdict=UpdateVerdict.REFUSED))

    state = store.get(agent_id)
    assert state.activity is UpdateActivity.STARTING
    assert state.verdict is None


def test_a_dismissed_outcome_is_not_resurrected_but_a_newer_runs_is_recorded(tmp_path: Path) -> None:
    """The dismissed record stays on disk and is re-read forever; a newer run's record must still land."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()
    started_at = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    store.observe_run_record(agent_id, _run_record(started_at=started_at, verdict=UpdateVerdict.STUCK))
    store.dismiss_run_outcome(agent_id)
    assert store.get(agent_id).verdict is None

    store.observe_run_record(agent_id, _run_record(started_at=started_at, verdict=UpdateVerdict.STUCK))
    assert store.get(agent_id).verdict is None

    newer = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)
    store.observe_run_record(agent_id, _run_record(started_at=newer, verdict=UpdateVerdict.UPDATED))
    assert store.get(agent_id).verdict is UpdateVerdict.UPDATED


def test_a_stalled_runs_own_record_does_not_re_enter_it_as_running(tmp_path: Path) -> None:
    """STALLED is a later reading than the record, which must not undo it on every sweep."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()
    started_at = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    store.observe_run_record(agent_id, _run_record(started_at=started_at))
    store.set_activity(agent_id, UpdateActivity.STALLED)

    store.observe_run_record(agent_id, _run_record(started_at=started_at))

    assert store.get(agent_id).activity is UpdateActivity.STALLED


def test_the_sweep_reads_the_run_record_only_for_reachable_idle_rows(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """An in-flight row's record is the poll's to read, and an unreachable host cannot answer the exec."""
    idle_id = AgentId.generate()
    in_flight_id = AgentId.generate()
    stopped_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={
            idle_id: HostState.RUNNING,
            in_flight_id: HostState.RUNNING,
            stopped_id: HostState.STOPPED,
        },
    )
    caller = _VersionReadingMngrCaller()
    read_ids: list[str] = []

    def read_run_record(agent_id: AgentId) -> UpdateRunStatus | None:
        read_ids.append(str(agent_id))
        return _run_record(verdict=UpdateVerdict.UPDATED)

    detector = _detector(tmp_path, resolver, caller, root_concurrency_group, read_run_record=read_run_record)
    detector.store.try_begin_run(in_flight_id, chat_agent_name="update-x")

    detector.run_pass()

    assert read_ids == [str(idle_id)]
    assert detector.store.get(idle_id).verdict is UpdateVerdict.UPDATED
