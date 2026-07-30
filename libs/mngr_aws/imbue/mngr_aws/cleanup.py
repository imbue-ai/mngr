"""AWS adapter for the shared, age-based VPS test-instance reaper.

Supplies AWS's ``CreatedAtExtractor`` (reads the ``mngr-created-at`` tag) and a thin
wrapper over the shared reaper in ``imbue.mngr_vps.leak_cleanup``.
"""

from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Final

from imbue.mngr_aws.client import AWS_PYTEST_LAUNCHED_TAG
from imbue.mngr_vps.leak_cleanup import VpsReaperClient
from imbue.mngr_vps.leak_cleanup import cleanup_old_test_instances
from imbue.mngr_vps.leak_cleanup import has_launched_marker
from imbue.mngr_vps.leak_cleanup import parse_iso_utc
from imbue.mngr_vps.leak_cleanup import parse_tag_value

AWS_CREATED_AT_TAG_KEY: Final[str] = "mngr-created-at"


def aws_test_created_at(instance: Mapping[str, Any]) -> datetime | None:
    """Return a pytest-launched EC2 instance's UTC creation time, or ``None`` to leave it alone."""
    if not has_launched_marker(instance, AWS_PYTEST_LAUNCHED_TAG):
        return None
    return parse_iso_utc(parse_tag_value(instance.get("tags", ()), AWS_CREATED_AT_TAG_KEY))


def cleanup_old_aws_test_instances(client: VpsReaperClient, max_age: timedelta, now: datetime) -> int:
    """Destroy pytest-launched EC2 instances older than ``max_age``; return the count cleaned up."""
    return cleanup_old_test_instances(client, aws_test_created_at, max_age, now)
