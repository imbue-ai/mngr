import json

import pytest

from imbue.mngr.cli.output_helpers import emit_error_event
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_imbue_cloud.errors import ImbueCloudQuotaExceededError


def test_a_quota_refusals_error_event_carries_the_limit_it_hit(capsys: pytest.CaptureFixture[str]) -> None:
    """The desktop names the plan limit from these keys, so a refused create needs no message parsing."""
    error = ImbueCloudQuotaExceededError(
        "Quota exceeded for max_remote_workspaces", entitlement="max_remote_workspaces", limit=2.0, current=2.0
    )

    emit_error_event(error, OutputFormat.JSONL)

    event = json.loads(capsys.readouterr().out.strip())
    assert event["error_class"] == "ImbueCloudQuotaExceededError"
    assert event["details"] == {"entitlement": "max_remote_workspaces", "limit": 2.0, "current": 2.0}
