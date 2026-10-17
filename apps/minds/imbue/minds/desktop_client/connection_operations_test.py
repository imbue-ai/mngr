from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.api_models import CloudAccountSummary
from imbue.minds.desktop_client.connection_operations import ConnectionOperationFailure
from imbue.minds.desktop_client.connection_operations import ConnectionOperationKind
from imbue.minds.desktop_client.connection_operations import ConnectionOperationRegistry
from imbue.minds.desktop_client.connection_operations import ConnectionOperationStatus
from imbue.minds.desktop_client.connection_operations import FINISHED_CONNECTION_OPERATION_RETENTION
from imbue.minds.errors import ConnectionOperationInProgressError

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

    sink = registry.begin(_INSTANCE, ConnectionOperationKind.ADD)
    running = registry.get(_INSTANCE)
    assert running is not None and running.status is ConnectionOperationStatus.RUNNING
    assert running.kind is ConnectionOperationKind.ADD and running.finished_at is None
    assert registry.get_log_sink(_INSTANCE) is sink
    # The instance is the operation id, so a second operation waits for the first.
    with pytest.raises(ConnectionOperationInProgressError):
        registry.begin(_INSTANCE, ConnectionOperationKind.UPDATE)

    failure = ConnectionOperationFailure(status_code=502, error="bad token", code="credentials_rejected")
    registry.finish_failed(_INSTANCE, failure)
    failed = registry.get(_INSTANCE)
    assert failed is not None and failed.status is ConnectionOperationStatus.FAILED
    assert failed.failure == failure and failed.account is None and failed.finished_at is not None

    # A finished operation no longer holds the instance: an update may begin, with a sink of its own.
    update_sink = registry.begin(_INSTANCE, ConnectionOperationKind.UPDATE)
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
        registry.begin(instance, ConnectionOperationKind.ADD)
        registry.finish_done(instance, _ACCOUNT)
    stale_record = registry.get(stale)
    assert stale_record is not None
    expired_at = datetime.now(timezone.utc) - FINISHED_CONNECTION_OPERATION_RETENTION - timedelta(seconds=1)
    registry._record_by_id[stale] = stale_record.model_copy_update(
        to_update(stale_record.field_ref().finished_at, expired_at)
    )

    registry.begin(_INSTANCE, ConnectionOperationKind.ADD)

    assert registry.get(stale) is None and registry.get_log_sink(stale) is None
    assert registry.get(fresh) is not None and registry.get_log_sink(fresh) is not None
    assert registry.get(_INSTANCE) is not None
