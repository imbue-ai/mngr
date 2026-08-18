import pytest
from pydantic import Field

from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.wire import WireModel
from imbue.mngr_imbue_cloud.wire import parse_wire_entries
from imbue.mngr_imbue_cloud.wire import validate_wire
from imbue.mngr_imbue_cloud.wire_types import R2BucketAccess
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus


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
