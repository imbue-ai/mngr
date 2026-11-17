import json

import pytest

from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.errors import GRANTS_REFUSED_CODE
from imbue.mngr_imbue_cloud.errors import ImbueCloudPlanRefusedError
from imbue.mngr_imbue_cloud.errors import ImbueCloudShareRefusedError


@handle_imbue_cloud_errors
def _switch_plan_refused_by_the_connector() -> None:
    raise ImbueCloudPlanRefusedError("The 'ally' plan requires partner access")


def test_a_plan_switch_the_connector_refused_fails_with_the_plan_refused_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The desktop tells a stated refusal from a failure worth retrying by this code, not by the message."""
    with pytest.raises(SystemExit) as exit_info:
        _switch_plan_refused_by_the_connector()

    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "The 'ally' plan requires partner access",
        "error_class": "ImbueCloudPlanRefusedError",
        "code": "plan_refused",
    }


_REFUSED_ENTRY = {
    "scope": "system_interface",
    "kind": "domain",
    "value": "gmail.com",
    "message": "gmail.com is a public email provider",
}


@handle_imbue_cloud_errors
def _push_refused_by_the_connector() -> None:
    raise ImbueCloudShareRefusedError(GRANTS_REFUSED_CODE, "The push adds a grant the rules forbid", [_REFUSED_ENTRY])


def test_a_refused_push_fails_with_the_grants_refused_code_and_names_each_refused_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The desktop shows the refused entries from this body, so the key and the entries' shape are its contract."""
    with pytest.raises(SystemExit) as exit_info:
        _push_refused_by_the_connector()

    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "The push adds a grant the rules forbid",
        "error_class": "ImbueCloudShareRefusedError",
        "code": GRANTS_REFUSED_CODE,
        "refusals": [_REFUSED_ENTRY],
    }
