from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.api_models import CloudAccountSummary
from imbue.minds.desktop_client.connection_operations import ConnectionOperationFailure
from imbue.minds.desktop_client.connection_operations import ConnectionOperationKind
from imbue.minds.desktop_client.connection_operations import ConnectionOperationRegistry
from imbue.minds.desktop_client.connection_operations import ConnectionOperationStatus
from imbue.minds.desktop_client.connection_operations import FINISHED_CONNECTION_OPERATION_RETENTION
from imbue.minds.desktop_client.create_progress import ProgressStepStatus
from imbue.minds.desktop_client.create_progress_history import ProgressHistoryStore
from imbue.minds.desktop_client.create_progress_history import ShippedProgressDefaults
from imbue.minds.desktop_client.create_progress_history import connection_scope
from imbue.minds.desktop_client.create_progress_history import parse_history_lines
from imbue.minds.errors import ConnectionOperationInProgressError
from imbue.mngr.primitives import ProgressStepId
from imbue.mngr.utils.progress import ProgressStep

_INSTANCE = "modal_" + "a" * 32

_ACCOUNT = CloudAccountSummary(
    name=_INSTANCE,
    alias="eval modal",
    backend="modal",
    region="",
    identifier="ak-t…1234",
    owner_user_id="local",
    is_region_pinned=False,
)


def test_operation_kinds_name_themselves_on_the_wire_as_the_handle_and_status_routes_carry_them() -> None:
    assert ConnectionOperationKind.ADD.wire_kind == "connection_add"
    assert ConnectionOperationKind.UPDATE.wire_kind == "connection_update"


def test_registry_claims_an_operation_until_it_ends_then_lets_the_connection_be_operated_on_again() -> None:
    registry = ConnectionOperationRegistry()
    assert registry.get(_INSTANCE) is None and registry.get_log_sink(_INSTANCE) is None

    sink = registry.begin(_INSTANCE, ConnectionOperationKind.ADD, "aws")
    running = registry.get(_INSTANCE)
    assert running is not None and running.status is ConnectionOperationStatus.RUNNING
    assert running.kind is ConnectionOperationKind.ADD and running.finished_at is None
    assert registry.get_log_sink(_INSTANCE) is sink
    # The instance is the operation id, so a second operation waits for the first.
    with pytest.raises(ConnectionOperationInProgressError):
        registry.begin(_INSTANCE, ConnectionOperationKind.UPDATE, "aws")

    failure = ConnectionOperationFailure(status_code=502, error="bad token", code="credentials_rejected")
    registry.finish_failed(_INSTANCE, failure)
    failed = registry.get(_INSTANCE)
    assert failed is not None and failed.status is ConnectionOperationStatus.FAILED
    assert failed.failure == failure and failed.account is None and failed.finished_at is not None

    # A finished operation no longer holds the instance: an update may begin, with a sink of its own.
    update_sink = registry.begin(_INSTANCE, ConnectionOperationKind.UPDATE, "aws")
    assert update_sink is not sink and registry.get_log_sink(_INSTANCE) is update_sink
    registry.finish_done(_INSTANCE, _ACCOUNT)
    done = registry.get(_INSTANCE)
    assert done is not None and done.status is ConnectionOperationStatus.DONE
    assert done.kind is ConnectionOperationKind.UPDATE and done.account == _ACCOUNT and done.failure is None


def test_registry_drops_finished_operations_past_their_retention_on_the_next_begin() -> None:
    registry = ConnectionOperationRegistry()
    stale = "aws_" + "b" * 32
    fresh = "gcp_" + "c" * 32
    for instance in (stale, fresh):
        registry.begin(instance, ConnectionOperationKind.ADD, "aws")
        registry.finish_done(instance, _ACCOUNT)
    stale_record = registry.get(stale)
    assert stale_record is not None
    expired_at = datetime.now(timezone.utc) - FINISHED_CONNECTION_OPERATION_RETENTION - timedelta(seconds=1)
    registry._record_by_id[stale] = stale_record.model_copy_update(
        to_update(stale_record.field_ref().finished_at, expired_at)
    )

    registry.begin(_INSTANCE, ConnectionOperationKind.ADD, "aws")

    assert registry.get(stale) is None and registry.get_log_sink(stale) is None
    assert registry.get(fresh) is not None and registry.get_log_sink(fresh) is not None
    assert registry.get(_INSTANCE) is not None


def test_registry_tracks_each_operations_progress_and_learns_its_durations(tmp_path: Path) -> None:
    store = ProgressHistoryStore(
        events_path=tmp_path / "events.jsonl",
        shipped_defaults=ShippedProgressDefaults(
            default_step_seconds=5.0, seconds_by_step_id_by_scope={"connection:aws": {"aws.state_bucket": 4.0}}
        ),
    )
    registry = ConnectionOperationRegistry(progress_history=store)
    assert registry.progress_snapshot(_INSTANCE) is None
    with pytest.raises(KeyError):
        registry.progress_tracker(_INSTANCE)

    registry.begin(_INSTANCE, ConnectionOperationKind.ADD, "aws")
    started = registry.progress_snapshot(_INSTANCE)
    assert started is not None and started.percent == 0.0 and started.current_label == ""
    tracker = registry.progress_tracker(_INSTANCE)
    tracker.announce_plan(
        [
            ProgressStep(step_id=ProgressStepId("aws.security_group"), label="Opening SSH access"),
            ProgressStep(step_id=ProgressStepId("aws.state_bucket"), label="Creating the state bucket"),
        ],
        None,
    )
    tracker.start_step("aws.security_group", "Opening SSH access", None, None)
    tracker.complete_step("aws.security_group")
    tracker.start_step("aws.state_bucket", "Creating the state bucket", None, None)
    running = registry.progress_snapshot(_INSTANCE)
    assert running is not None and running.current_label == "Creating the state bucket"
    assert 0.0 < running.percent < 100.0

    registry.finish_done(_INSTANCE, _ACCOUNT)

    done = registry.progress_snapshot(_INSTANCE)
    assert done is not None and done.percent == 100.0 and done.remaining_text == ""
    assert [step.status for step in done.steps] == [ProgressStepStatus.DONE, ProgressStepStatus.DONE]
    # Both steps measured something, so the next aws connection weighs its bar by them.
    learned = store.expected_durations(connection_scope("aws")).seconds_by_step_id
    assert {"aws.security_group", "aws.state_bucket"} <= set(learned)
    assert all(seconds >= 0.0 for seconds in learned.values())

    # A failed operation keeps what its finished steps measured and nothing from the one left running.
    failed_instance = "aws_" + "d" * 32
    registry.begin(failed_instance, ConnectionOperationKind.ADD, "aws")
    failing = registry.progress_tracker(failed_instance)
    failing.start_step("aws.security_group", "Opening SSH access", None, None)
    failing.complete_step("aws.security_group")
    failing.start_step("aws.state_bucket", "Creating the state bucket", None, None)
    registry.finish_failed(failed_instance, ConnectionOperationFailure(status_code=502, error="x", code="y"))
    recorded = parse_history_lines((tmp_path / "events.jsonl").read_text().splitlines(), connection_scope("aws"))
    assert len(recorded["aws.security_group"]) == 2
    assert len(recorded["aws.state_bucket"]) == 1
    failed_snapshot = registry.progress_snapshot(failed_instance)
    assert failed_snapshot is not None and failed_snapshot.percent < 100.0
