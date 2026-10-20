"""Detection: version comparison, the tri-state, and how the composed state is assembled."""

import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.desktop_client.testing import landed_verdict
from imbue.minds.desktop_client.testing import make_update_state_store
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.update_dismissal_store import UpdateDismissalRecord
from imbue.minds.desktop_client.update_dismissal_store import UpdateDismissalStore
from imbue.minds.desktop_client.update_status import FAILURE_UPDATE_VERDICTS
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateDispatchFailure
from imbue.minds.desktop_client.update_status import UpdateLastDispatch
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateScheduleRecord
from imbue.minds.desktop_client.update_status import UpdateStartedBy
from imbue.minds.desktop_client.update_status import UpdateUnknownReason
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.update_workspace_command import UpdateWorkspaceFacts
from imbue.minds.desktop_client.workspace_update_state import AutoUpdatePolicy
from imbue.minds.desktop_client.workspace_update_state import ORIGINAL_MINDS_VERSION_LABEL
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateDetector
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.desktop_client.workspace_update_state import derive_update_detection
from imbue.minds.desktop_client.workspace_update_state import is_below_in_place_update_floor
from imbue.minds.desktop_client.workspace_update_state import resolve_workspace_version
from imbue.minds.desktop_client.workspace_update_state import should_auto_arm
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


@pytest.mark.witnesses("workspace-updates.minor-release-granularity")
@pytest.mark.parametrize(
    ("workspace_ref", "ceiling_ref", "expected"),
    [
        ("minds-v0.8.3", "minds-v0.9.0", UpdateAvailability.OUT_OF_DATE),
        ("minds-v0.7.9", "minds-v0.8.4", UpdateAvailability.OUT_OF_DATE),
        ("minds-v0.9.5", "minds-v1.0.0", UpdateAvailability.OUT_OF_DATE),
        ("minds-v0.8.3", "minds-v0.9.0-rc.1", UpdateAvailability.OUT_OF_DATE),
        ("minds-v0.8.3", "minds-v0.8.4", UpdateAvailability.PATCH_AVAILABLE),
        ("minds-v0.8.0", "minds-v0.8.4", UpdateAvailability.PATCH_AVAILABLE),
        ("minds-v0.9.0-rc.1", "minds-v0.9.0", UpdateAvailability.PATCH_AVAILABLE),
        ("minds-v0.8.4", "minds-v0.8.3", UpdateAvailability.APP_BEHIND),
    ],
)
def test_only_a_newer_minor_release_makes_a_workspace_out_of_date(
    workspace_ref: str, ceiling_ref: str, expected: UpdateAvailability
) -> None:
    detection = derive_update_detection(workspace_ref, ceiling_ref)
    assert detection.availability is expected
    assert detection.unknown_reason is None


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


@pytest.mark.parametrize("workspace_ref", [None, "", "main", "gabriel/some-branch", "my-own-template"])
def test_a_version_nobody_could_read_is_never_below_the_in_place_floor(workspace_ref: str | None) -> None:
    """The floor refuses mutations, so an unreadable version must not trip it.

    The backup-service update refuses a machine below the floor outright; if an
    unparseable ref counted as below it, every machine on a branch template --
    and every machine the detector has not resolved yet -- would lose that
    update.
    """
    assert is_below_in_place_update_floor(workspace_ref) is False


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


def test_a_patch_is_never_offered_but_may_be_run_by_hand() -> None:
    patch_available = _state_with(UpdateAvailability.PATCH_AVAILABLE)
    assert patch_available.is_update_offered is False
    assert patch_available.is_update_dispatchable is True


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
        return MngrCallResult(returncode=0, stdout=exec_json_envelope(stdout))

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
    read_update_facts: Callable[[AgentId], UpdateWorkspaceFacts | None] | None = None,
    auto_update_policy: AutoUpdatePolicy | None = None,
    armed: list[AgentId] | None = None,
) -> WorkspaceUpdateDetector:
    armed_ids = armed if armed is not None else []
    return WorkspaceUpdateDetector(
        store=store if store is not None else make_update_state_store(tmp_path),
        backend_resolver=resolver,
        mngr_caller=caller,
        concurrency_group=concurrency_group,
        read_supported_version=lambda: "minds-v0.4.1",
        interval_seconds=interval_seconds,
        read_update_facts=read_update_facts if read_update_facts is not None else lambda _agent_id: None,
        read_auto_update_policy=lambda _agent_id: (
            auto_update_policy
            if auto_update_policy is not None
            else AutoUpdatePolicy(is_enabled=False, skipped_target_ref="")
        ),
        arm_for_auto_update=armed_ids.append,
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


@pytest.mark.witnesses("workspace-updates.read-held-while-unreadable")
def test_a_machine_that_became_unreadable_keeps_the_version_it_was_read_at(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Its git cannot move while nothing runs in it, and the label is only its birth version.

    Pressing Update on a running machine announces it as STARTING before the
    no-op start returns; the badge must not drop to the create-time version for
    the length of that announcement.
    """
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={agent_id: HostState.RUNNING},
        label_by_agent={str(agent_id): "minds-v0.3.10"},
    )
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.4.1"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)
    detector.run_pass()

    resolver.host_state_by_agent = {agent_id: HostState.STARTING}
    detector.run_pass()

    state = detector.store.get(agent_id)
    assert state.current_version == "minds-v0.4.1"
    assert state.is_version_from_label is False
    assert caller.read_agent_ids == [str(agent_id)]


def test_a_machine_readable_again_is_re_read_whatever_the_cache_s_age(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A version it was read at before going away is served meanwhile, not trusted afterwards."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(url_by_agent_and_service={}, host_state_by_agent={agent_id: HostState.RUNNING})
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.3.12"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group)
    detector.run_pass()
    resolver.host_state_by_agent = {agent_id: HostState.STOPPED}
    detector.run_pass()

    resolver.host_state_by_agent = {agent_id: HostState.RUNNING}
    caller.version_by_agent = {str(agent_id): "minds-v0.4.1"}
    detector.run_pass()

    assert caller.read_agent_ids == [str(agent_id), str(agent_id)]
    assert detector.store.get(agent_id).current_version == "minds-v0.4.1"


@pytest.mark.witnesses("workspace-updates.read-held-past-a-failed-read")
def test_a_read_that_failed_keeps_the_version_already_read(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """An exec that timed out under slow discovery says nothing about the version; the label would say less."""
    agent_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={agent_id: HostState.RUNNING},
        label_by_agent={str(agent_id): "minds-v0.3.10"},
    )
    caller = _VersionReadingMngrCaller(version_by_agent={str(agent_id): "minds-v0.4.1"})
    detector = _detector(tmp_path, resolver, caller, root_concurrency_group, interval_seconds=0.0)
    detector.run_pass()

    caller.version_by_agent = {}
    detector.run_pass()

    state = detector.store.get(agent_id)
    assert caller.read_agent_ids == [str(agent_id), str(agent_id)]
    assert state.current_version == "minds-v0.4.1"
    assert state.is_version_from_label is False


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


# The workspace's own run record
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
    assert store.try_begin_run(agent_id, target_override="")

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


def test_a_dismissed_outcome_stays_dismissed_after_a_relaunch(tmp_path: Path) -> None:
    """A relaunched app re-reads the same run record on its first sweep."""
    agent_id = AgentId.generate()
    started_at = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    first_launch = make_update_state_store(tmp_path)
    first_launch.observe_run_record(agent_id, _run_record(started_at=started_at, verdict=UpdateVerdict.STUCK))
    first_launch.dismiss_run_outcome(agent_id)

    relaunched = make_update_state_store(tmp_path)
    relaunched.observe_run_record(agent_id, _run_record(started_at=started_at, verdict=UpdateVerdict.STUCK))
    assert relaunched.get(agent_id).verdict is None

    newer = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)
    relaunched.observe_run_record(agent_id, _run_record(started_at=newer, verdict=UpdateVerdict.REFUSED))
    assert relaunched.get(agent_id).verdict is UpdateVerdict.REFUSED


def test_a_dismissed_stall_does_not_come_back_as_a_running_run_after_a_relaunch(tmp_path: Path) -> None:
    """A stall's record is verdictless, so a forgotten dismissal would re-enter it as in flight and lock the row."""
    agent_id = AgentId.generate()
    started_at = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    first_launch = make_update_state_store(tmp_path)
    first_launch.observe_run_record(agent_id, _run_record(started_at=started_at))
    first_launch.set_activity(agent_id, UpdateActivity.STALLED)
    first_launch.dismiss_run_outcome(agent_id)

    relaunched = make_update_state_store(tmp_path)
    relaunched.observe_run_record(agent_id, _run_record(started_at=started_at))

    state = relaunched.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.is_run_in_flight is False

    newer = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)
    relaunched.observe_run_record(agent_id, _run_record(chat="update-newer", started_at=newer))
    assert relaunched.get(agent_id).activity is UpdateActivity.RUNNING


def test_dismissing_a_landed_runs_outcome_keeps_its_note_after_a_relaunch(tmp_path: Path) -> None:
    """The badge and the note are dismissed separately, so a relaunch must not let one take the other."""
    agent_id = AgentId.generate()
    record = _run_record(verdict=UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1")
    first_launch = make_update_state_store(tmp_path)
    first_launch.observe_run_record(agent_id, record)
    first_launch.dismiss_run_outcome(agent_id)

    relaunched = make_update_state_store(tmp_path)
    relaunched.observe_run_record(agent_id, record)
    relaunched.observe_run_record(agent_id, record)

    state = relaunched.get(agent_id)
    assert state.success_note_version == "minds-v0.4.1"
    assert state.verdict is None


@pytest.mark.witnesses("workspace-updates.updated-note", partial="the dismissal outliving a relaunch")
def test_a_dismissed_note_stays_dismissed_after_a_relaunch_without_clearing_the_outcome(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    record = _run_record(verdict=UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS, resulting_ref="minds-v0.4.1")
    first_launch = make_update_state_store(tmp_path)
    first_launch.observe_run_record(agent_id, record)
    first_launch.dismiss_success_note(agent_id)

    relaunched = make_update_state_store(tmp_path)
    relaunched.observe_run_record(agent_id, record)

    state = relaunched.get(agent_id)
    assert state.success_note_version == ""
    assert state.verdict is UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS


def test_dismissing_an_earlier_runs_note_during_a_newer_run_keeps_the_newer_runs_note(tmp_path: Path) -> None:
    """The note outlives its record once a newer run starts; the dismissal must name the run that earned it."""
    agent_id = AgentId.generate()
    first_launch = make_update_state_store(tmp_path)
    first_launch.observe_run_record(
        agent_id,
        _run_record(
            started_at=datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc),
            verdict=UpdateVerdict.UPDATED,
            resulting_ref="minds-v0.4.1",
        ),
    )
    assert first_launch.try_begin_run(agent_id, target_override="")
    first_launch.dismiss_success_note(agent_id)

    relaunched = make_update_state_store(tmp_path)
    relaunched.observe_run_record(
        agent_id,
        _run_record(
            chat="update-newer",
            started_at=datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc),
            verdict=UpdateVerdict.UPDATED,
            resulting_ref="minds-v0.5.0",
        ),
    )

    assert relaunched.get(agent_id).success_note_version == "minds-v0.5.0"


# Bounds the park in both directions: long enough that a dismissal racing an unlocked
# read always gets through, short enough that one blocked behind the lock does not hang.
_PARK_SECONDS: Final[float] = 1.0


class _ParkableDismissalStore(UpdateDismissalStore):
    """A dismissal store whose read can be parked once, holding a sweep where it consults the dismissals."""

    _is_park_armed: threading.Event = PrivateAttr(default_factory=threading.Event)
    _is_parked: threading.Event = PrivateAttr(default_factory=threading.Event)
    _is_park_over: threading.Event = PrivateAttr(default_factory=threading.Event)

    def arm_park(self) -> None:
        self._is_park_armed.set()

    def wait_until_parked(self) -> None:
        assert self._is_parked.wait(timeout=_PARK_SECONDS), "the sweep never reached its dismissal read"

    def end_park(self) -> None:
        self._is_park_over.set()

    def read(self, agent_id: AgentId) -> UpdateDismissalRecord:
        record = super().read(agent_id)
        if self._is_park_armed.is_set():
            self._is_park_armed.clear()
            self._is_parked.set()
            self._is_park_over.wait(timeout=_PARK_SECONDS)
        return record


def test_a_dismissal_landing_while_a_sweep_reads_the_dismissals_is_not_undone(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The dismissals a sweep decides from and the row it writes have to come from one lock hold.

    A dismissal persists the run and clears the row under that lock; a sweep that read the
    store before the write and reached the row after it would put the badge straight back.
    """
    agent_id = AgentId.generate()
    dismissal_store = _ParkableDismissalStore(records_dir=tmp_path / "update_dismissals")
    store = make_update_state_store(tmp_path, dismissal_store=dismissal_store)
    record = _run_record(verdict=UpdateVerdict.STUCK)
    store.observe_run_record(agent_id, record)
    is_dismissed = threading.Event()

    def dismiss() -> None:
        store.dismiss_run_outcome(agent_id)
        is_dismissed.set()

    dismissal_store.arm_park()
    with root_concurrency_group.make_concurrency_group("dismissal-race") as race:
        race.start_new_thread(target=store.observe_run_record, args=(agent_id, record), name="sweep")
        dismissal_store.wait_until_parked()
        race.start_new_thread(target=dismiss, name="dismiss")
        # Held off by the sweep's lock hold, this waits out; unlocked, it lands here.
        is_dismissed.wait(timeout=_PARK_SECONDS)
        dismissal_store.end_park()

    assert store.get(agent_id).verdict is None


def test_a_stalled_runs_own_record_does_not_re_enter_it_as_running(tmp_path: Path) -> None:
    """STALLED is a later reading than the record, which must not undo it on every sweep."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()
    started_at = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    store.observe_run_record(agent_id, _run_record(started_at=started_at))
    store.set_activity(agent_id, UpdateActivity.STALLED)

    store.observe_run_record(agent_id, _run_record(started_at=started_at))

    assert store.get(agent_id).activity is UpdateActivity.STALLED


def test_the_sweep_reads_reachable_rows_and_leaves_an_in_flight_rows_record_to_the_poll(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """An in-flight row's record is the poll's to adopt, and an unreachable host cannot answer the exec."""
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

    def read_update_facts(agent_id: AgentId) -> UpdateWorkspaceFacts | None:
        read_ids.append(str(agent_id))
        return UpdateWorkspaceFacts(
            schedule=None, run_status=_run_record(verdict=UpdateVerdict.UPDATED), last_dispatch=None, is_signed_in=None
        )

    detector = _detector(tmp_path, resolver, caller, root_concurrency_group, read_update_facts=read_update_facts)
    detector.store.try_begin_run(in_flight_id, target_override="")

    detector.run_pass()

    assert sorted(read_ids) == sorted([str(idle_id), str(in_flight_id)])
    assert detector.store.get(idle_id).verdict is UpdateVerdict.UPDATED
    assert detector.store.get(in_flight_id).verdict is None


_UNREACHED_FAILURE = UpdateDispatchFailure(
    message="Couldn't reach this workspace to start the update.", detail="Error: connection refused"
)


def test_only_a_run_started_after_a_failed_dispatch_clears_it_from_outside_the_app(tmp_path: Path) -> None:
    """The sweep re-reads the machine's last run record every pass; an earlier run's says nothing about the attempt."""
    store, agent_id = _out_of_date_store(tmp_path)
    assert store.try_begin_run(agent_id, target_override="")
    claimed_at = store.get(agent_id).run_started_at
    assert claimed_at is not None
    assert store.release_failed_claim(agent_id, _UNREACHED_FAILURE) is True

    store.observe_run_record(
        agent_id,
        _run_record(chat="update-earlier", started_at=claimed_at - timedelta(days=1), verdict=UpdateVerdict.UPDATED),
    )
    assert store.get(agent_id).dispatch_failure == "Couldn't reach this workspace to start the update."

    store.observe_run_record(
        agent_id, _run_record(chat="update-by-hand", started_at=claimed_at + timedelta(minutes=5))
    )

    state = store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.dispatch_failure == ""
    assert state.dispatch_failure_detail == ""


def test_a_failed_dispatch_whose_run_the_poll_already_entered_records_no_failure(tmp_path: Path) -> None:
    """A spawn that timed out after creating its chat has a live run; a failure recorded over it would be false."""
    store, agent_id = _out_of_date_store(tmp_path)
    assert store.try_begin_run(agent_id, target_override="")
    store.set_activity(agent_id, UpdateActivity.RUNNING)

    assert store.release_failed_claim(agent_id, _UNREACHED_FAILURE) is False

    state = store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.dispatch_failure == ""


def test_a_declined_bulk_attempt_is_not_recorded_over_a_run_in_flight(tmp_path: Path) -> None:
    """A run that started between the gate's reading and the record is the answer to the request."""
    store, agent_id = _out_of_date_store(tmp_path)
    assert store.try_begin_run(agent_id, target_override="")

    assert store.record_skipped_dispatch(agent_id, _UNREACHED_FAILURE) is False

    assert store.get(agent_id).dispatch_failure == ""


def _schedule_record(**overrides: object) -> UpdateScheduleRecord:
    fields: dict[str, object] = {
        "armed_at": datetime(2026, 10, 19, tzinfo=timezone.utc),
        "ceiling_ref": "minds-v0.4.1",
        "window_start_hour": 2,
        "window_end_hour": 5,
        "zone": "America/Los_Angeles",
        "override_ref": "",
        "marked_chat_name": "",
        "pending_skip_reason": "",
        "last_skip_reason": "",
        "last_skip_at": None,
    }
    fields.update(overrides)
    return UpdateScheduleRecord.model_validate(fields)


def test_the_row_is_scheduled_exactly_as_the_workspaces_file_says(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.record_schedule(
        agent_id,
        schedule=_schedule_record(override_ref="minds-v0.4.2", last_skip_reason="LEASE_HELD"),
        last_dispatch=None,
        is_signed_in=None,
    )

    state = store.get(agent_id)
    assert state.is_scheduled is True
    assert (state.override_ref, state.schedule_window, state.schedule_zone) == (
        "minds-v0.4.2",
        "2:00 AM-5:00 AM",
        "America/Los_Angeles",
    )
    assert state.last_skip_reason == "Another update flow was working in this workspace during the last update window."

    store.record_schedule(agent_id, schedule=None, last_dispatch=None, is_signed_in=None)

    assert store.get(agent_id).is_scheduled is False


@pytest.mark.witnesses("workspace-updates.needs-sign-in", partial="the row state; not the sign-in button")
def test_a_sign_in_skip_reads_as_needing_sign_in_until_a_read_finds_an_account(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.record_schedule(
        agent_id,
        schedule=_schedule_record(pending_skip_reason="NEEDS_SIGN_IN"),
        last_dispatch=None,
        is_signed_in=False,
    )
    assert store.get(agent_id).needs_sign_in is True

    store.record_schedule(
        agent_id, schedule=_schedule_record(pending_skip_reason="NEEDS_SIGN_IN"), last_dispatch=None, is_signed_in=True
    )

    assert store.get(agent_id).needs_sign_in is False


def test_a_refused_arm_reads_as_needing_sign_in_until_an_arm_lands(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.record_sign_in_refusal(agent_id, is_refused=True)
    assert store.get(agent_id).needs_sign_in is True

    store.record_armed_schedule(agent_id, _schedule_record())

    state = store.get(agent_id)
    assert state.needs_sign_in is False
    assert state.is_scheduled is True


def test_a_scheduled_runs_start_kind_and_chat_come_from_its_dispatch_when_its_record_predates_them(
    tmp_path: Path,
) -> None:
    """An older template's run record says neither; the dispatch that created the chat does."""
    store, agent_id = _out_of_date_store(tmp_path)
    store.observe_run_record(agent_id, _run_record(chat="update-to-minds-v0-4-1"))
    store.record_schedule(
        agent_id,
        schedule=None,
        last_dispatch=UpdateLastDispatch(
            chat_agent_name="update-to-minds-v0-4-1",
            chat_id="agent-" + "a" * 32,
            started_by=UpdateStartedBy.SCHEDULED,
            ceiling_ref="minds-v0.4.1",
        ),
        is_signed_in=None,
    )

    state = store.get(agent_id)
    assert state.started_by is UpdateStartedBy.SCHEDULED
    assert state.chat_agent_id == "agent-" + "a" * 32


@pytest.mark.witnesses("workspace-updates.decision-pending", partial="the row state; not the modal")
def test_a_needs_decision_verdict_is_a_pending_decision_and_not_a_failure(tmp_path: Path) -> None:
    store, agent_id = _out_of_date_store(tmp_path)
    store.observe_run_record(agent_id, _run_record(verdict=UpdateVerdict.NEEDS_DECISION))

    state = store.get(agent_id)
    assert state.is_decision_pending is True
    assert state.verdict not in FAILURE_UPDATE_VERDICTS


_ENABLED = AutoUpdatePolicy(is_enabled=True, skipped_target_ref="")


def _offered_state(**overrides: object) -> UiWorkspaceUpdate:
    fields: dict[str, object] = {
        "availability": UpdateAvailability.OUT_OF_DATE,
        "current_version": "minds-v0.3.12",
        "supported_version": "minds-v0.4.1",
        "is_version_from_label": False,
        "activity": UpdateActivity.IDLE,
    }
    fields.update(overrides)
    return UiWorkspaceUpdate.model_validate(fields)


@pytest.mark.parametrize(
    ("state", "policy", "is_armed"),
    [
        (_offered_state(), _ENABLED, True),
        (_offered_state(), AutoUpdatePolicy(is_enabled=False, skipped_target_ref=""), False),
        # A patch rides along with the next minor release.
        (_offered_state(availability=UpdateAvailability.PATCH_AVAILABLE), _ENABLED, False),
        (_offered_state(is_scheduled=True), _ENABLED, False),
        (_offered_state(activity=UpdateActivity.RUNNING), _ENABLED, False),
        (_offered_state(verdict=UpdateVerdict.NEEDS_DECISION, is_decision_pending=True), _ENABLED, False),
        (_offered_state(), AutoUpdatePolicy(is_enabled=True, skipped_target_ref="minds-v0.4.1"), False),
        # A release skipped earlier does not hold back a newer one.
        (_offered_state(), AutoUpdatePolicy(is_enabled=True, skipped_target_ref="minds-v0.4.0"), True),
        # A run that failed after its lead's best effort is not retried unwatched.
        (_offered_state(verdict=UpdateVerdict.STUCK, chat_agent_name="update-to-minds-v0-4-1"), _ENABLED, False),
        # So is one that stalled with no verdict, which every surface reports as failed.
        (_offered_state(activity=UpdateActivity.STALLED, chat_agent_name="update-to-minds-v0-4-1"), _ENABLED, False),
    ],
)
@pytest.mark.witnesses("workspace-updates.arms-behind-workspaces")
@pytest.mark.witnesses("workspace-updates.skip-this-update", partial="the arming decision only")
def test_auto_update_arms_only_a_minor_release_behind_with_nothing_in_the_way(
    state: UiWorkspaceUpdate, policy: AutoUpdatePolicy, is_armed: bool
) -> None:
    assert should_auto_arm(state, policy, "minds-v0.4.1", last_dispatch=None) is is_armed


@pytest.mark.witnesses(
    "workspace-updates.failed-run-not-rescheduled", partial="the newer-release half; not the dismissal half"
)
def test_auto_update_arms_again_after_a_failure_once_the_ceiling_has_moved() -> None:
    failed = _offered_state(verdict=UpdateVerdict.REFUSED, chat_agent_name="update-to-minds-v0-4-0")
    dispatch = UpdateLastDispatch(
        chat_agent_name="update-to-minds-v0-4-0",
        chat_id="",
        started_by=UpdateStartedBy.SCHEDULED,
        ceiling_ref="minds-v0.4.0",
    )

    assert should_auto_arm(failed, _ENABLED, "minds-v0.4.0", dispatch) is False
    assert should_auto_arm(failed, _ENABLED, "minds-v0.4.1", dispatch) is True


@pytest.mark.witnesses("workspace-updates.arms-behind-workspaces")
@pytest.mark.witnesses("workspace-updates.stopped-workspace-not-updated", partial="the row while stopped")
def test_the_sweep_arms_a_behind_workspace_under_auto_update_and_shows_a_stopped_one_unscheduled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    behind_id = AgentId.generate()
    stopped_id = AgentId.generate()
    resolver = _WorkspacesResolver(
        url_by_agent_and_service={},
        host_state_by_agent={behind_id: HostState.RUNNING, stopped_id: HostState.STOPPED},
    )
    caller = _VersionReadingMngrCaller(version_by_agent={str(behind_id): "minds-v0.3.12"})
    armed: list[AgentId] = []
    store = make_update_state_store(tmp_path)
    store.record_schedule(stopped_id, schedule=_schedule_record(), last_dispatch=None, is_signed_in=None)
    detector = _detector(
        tmp_path,
        resolver,
        caller,
        root_concurrency_group,
        store=store,
        read_update_facts=lambda _agent_id: UpdateWorkspaceFacts(
            schedule=None, run_status=None, last_dispatch=None, is_signed_in=None
        ),
        auto_update_policy=_ENABLED,
        armed=armed,
    )

    detector.run_pass()

    assert armed == [behind_id]
    assert store.get(stopped_id).is_scheduled is False
