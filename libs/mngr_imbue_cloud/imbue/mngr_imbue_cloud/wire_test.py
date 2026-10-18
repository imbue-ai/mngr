import pytest
from pydantic import Field
from pydantic import ValidationError

from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.wire import WireModel
from imbue.mngr_imbue_cloud.wire import parse_wire_entries
from imbue.mngr_imbue_cloud.wire import validate_wire
from imbue.mngr_imbue_cloud.wire_types import LeaseResult
from imbue.mngr_imbue_cloud.wire_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.wire_types import PublishedPortInfo
from imbue.mngr_imbue_cloud.wire_types import PublishedPortProtocol
from imbue.mngr_imbue_cloud.wire_types import R2BucketAccess
from imbue.mngr_imbue_cloud.wire_types import SyncDocument
from imbue.mngr_imbue_cloud.wire_types import WorkspaceInfo
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStopKind


class _ExampleWireModel(WireModel):
    """Minimal wire model for exercising the tolerant-parse contract."""

    name: str = Field(description="A required field")
    count: int = Field(default=0, description="An optional field")


def test_wire_model_ignores_unknown_fields() -> None:
    parsed = validate_wire(_ExampleWireModel, {"name": "a", "count": 2, "added_in_a_newer_server": True})
    assert parsed.name == "a"
    assert parsed.count == 2
    assert not hasattr(parsed, "added_in_a_newer_server")


def test_wire_model_still_requires_required_fields() -> None:
    with pytest.raises(ValueError):
        validate_wire(_ExampleWireModel, {"count": 2})


def test_wire_enum_coerces_unrecognized_value_to_unknown() -> None:
    assert WorkspaceStatus("migrating") is WorkspaceStatus.UNKNOWN
    assert R2BucketAccess("append-only") is R2BucketAccess.UNKNOWN


def test_workspace_stop_kind_coerces_and_defaults_to_absent() -> None:
    assert WorkspaceStopKind("maintenance") is WorkspaceStopKind.MAINTENANCE
    assert WorkspaceStopKind("retired") is WorkspaceStopKind.RETIRED
    assert WorkspaceStopKind("quarantine") is WorkspaceStopKind.UNKNOWN
    entry = validate_wire(
        WorkspaceInfo,
        {
            "host_db_id": "11111111-2222-3333-4444-555555555555",
            "status": "stopped",
            "agent_id": "a",
            "host_id": "h",
            "host_name": "n",
        },
    )
    assert entry.stop_kind is None
    held = validate_wire(
        WorkspaceInfo,
        {
            "host_db_id": "11111111-2222-3333-4444-555555555555",
            "status": "stopped",
            "agent_id": "a",
            "host_id": "h",
            "host_name": "n",
            "stop_kind": "suspension",
        },
    )
    assert held.stop_kind is WorkspaceStopKind.SUSPENSION


def test_wire_enum_normalizes_case_and_whitespace_before_unknown() -> None:
    assert R2BucketAccess("ReadWrite") is R2BucketAccess.READWRITE
    assert R2BucketAccess(" READ ") is R2BucketAccess.READ
    assert WorkspaceStatus("RUNNING") is WorkspaceStatus.RUNNING


def test_parse_wire_entries_skips_one_bad_entry() -> None:
    entries = [{"name": "a"}, {"count": "not-an-int"}, {"name": "b"}]
    parsed = parse_wire_entries(_ExampleWireModel, entries, "GET /example", ImbueCloudConnectorError)
    assert [entry.name for entry in parsed] == ["a", "b"]


def test_parse_wire_entries_raises_when_every_entry_fails() -> None:
    entries = [{"unrelated": 1}, {"unrelated": 2}]
    with pytest.raises(ImbueCloudConnectorError, match="all 2 entries failed"):
        parse_wire_entries(_ExampleWireModel, entries, "GET /example", ImbueCloudConnectorError)


def test_parse_wire_entries_raises_on_non_list_body() -> None:
    with pytest.raises(ImbueCloudConnectorError, match="expected a JSON list"):
        parse_wire_entries(_ExampleWireModel, {"records": []}, "GET /example", ImbueCloudConnectorError)


def test_parse_wire_entries_accepts_empty_list() -> None:
    assert parse_wire_entries(_ExampleWireModel, [], "GET /example", ImbueCloudConnectorError) == []


def test_lease_result_published_ports_are_additive_and_coerce_unknown_protocols() -> None:
    # The field is absent from a connector that predates published ports and
    # from a row baked without them; both read as none.
    base = {
        "host_db_id": "11111111-2222-3333-4444-555555555555",
        "vps_address": "10.0.0.1",
        "ssh_port": 22000,
        "ssh_user": "root",
        "container_ssh_port": 22001,
        "agent_id": "a",
        "host_id": "h",
        "host_name": "n",
    }
    assert validate_wire(LeaseResult, base).published_ports == ()
    with_ports = validate_wire(
        LeaseResult,
        {
            **base,
            "published_ports": [
                {"guest_port": 59000, "protocol": "udp", "host_port": 22002},
                {"guest_port": 59000, "protocol": "sctp", "host_port": 22002},
            ],
        },
    )
    assert with_ports.published_ports == (
        PublishedPortInfo(guest_port=59000, protocol=PublishedPortProtocol.UDP, host_port=22002),
        PublishedPortInfo(guest_port=59000, protocol=PublishedPortProtocol.UNKNOWN, host_port=22002),
    )
    info = validate_wire(
        WorkspaceInfo,
        {"host_db_id": base["host_db_id"], "status": "running", "agent_id": "a", "host_id": "h", "host_name": "n"},
    )
    assert info.published_ports == ()
    assert validate_wire(LeasedHostInfo, {**base, "leased_at": "2026-01-01T00:00:00Z"}).published_ports == ()


def test_sync_document_requires_metadata() -> None:
    """A push sends only set fields and the server replaces metadata whole, so an unset metadata must not validate."""
    with pytest.raises(ValidationError):
        SyncDocument.model_validate({"kind": "connection", "doc_id": "conn-1", "state": "active", "revision": 1})
