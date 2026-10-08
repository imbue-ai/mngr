import json
from typing import Final

import pytest

from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.filed_permission_requests import FiledPermissionRequestsError
from imbue.mngr_latchkey.filed_permission_requests import parse_filed_permission_requests

_RECORD: Final[dict[str, object]] = {
    "request_id": "req-7a1c",
    "devices": "*",
    "created_at": "2026-10-07T09:00:00.000Z",
    "body": {"agent_id": "agent-" + "0" * 32, "rationale": "please", "type": "accounts", "payload": {}},
}


def test_a_record_with_a_field_this_build_does_not_know_is_read_by_the_fields_it_does() -> None:
    """A newer machine package may add to the record; an older desktop reads what it knows and syncs on."""
    (filed,) = parse_filed_permission_requests(json.dumps([{**_RECORD, "filed_by": "agent-container-9"}]))

    assert filed.request_id == "req-7a1c"
    assert filed.is_for_desktop(DesktopDeviceId("desktop-anyone"))
    assert filed.body["rationale"] == "please"


def test_a_machine_keeping_no_requests_answers_with_an_empty_array() -> None:
    assert parse_filed_permission_requests("[]") == ()


@pytest.mark.parametrize(
    ("records_json", "message"),
    [
        ("not json", "are not JSON"),
        (json.dumps(_RECORD), "are not the JSON array they should be"),
        (json.dumps([{**_RECORD, "devices": 7}]), "is not in the shape the machine writes"),
    ],
    ids=["not-json", "not-an-array", "record-out-of-shape"],
)
def test_an_answer_this_build_cannot_read_is_refused_rather_than_read_as_empty(
    records_json: str, message: str
) -> None:
    """An empty listing would have a desktop drop its pending requests as answered elsewhere, so a broken one raises."""
    with pytest.raises(FiledPermissionRequestsError, match=message):
        parse_filed_permission_requests(records_json)
