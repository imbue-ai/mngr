import json

import pytest

from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.errors import ImbueCloudPlanRefusedError


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
