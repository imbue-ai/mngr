"""AWS adapter for the shared, age-based VPS test-instance reaper.

The reaper logic lives in ``imbue.mngr_vps.leak_cleanup`` (shared across every VPS provider).
This module supplies only AWS's plumbing: the ``mngr-created-at`` tag ``AwsVpsClient`` stamps on
every instance and the ``mngr-pytest-launched`` marker it adds while ``PYTEST_CURRENT_TEST`` is
set, plus the matching ``CreatedAtExtractor`` that reads them back from ``list_instances()``.

An instance is reapable only when it carries the pytest-launched marker (so production instances
are never touched) and a parseable ``mngr-created-at`` timestamp. Driven by the session-end hook
in ``conftest.py`` and the standalone CI reaper ``scripts/cleanup_old_aws_test_instances.py``.
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

# ISO ``mngr-created-at`` tag ``AwsVpsClient.create_instance`` stamps on every instance.
AWS_CREATED_AT_TAG_KEY: Final[str] = "mngr-created-at"


def aws_test_created_at(instance: Mapping[str, Any]) -> datetime | None:
    """Return a pytest-launched EC2 instance's UTC creation time, or ``None`` to leave it alone.

    Reapable iff the instance carries the ``mngr-pytest-launched`` marker (so production instances
    are never matched) and a parseable ISO ``mngr-created-at`` tag.
    """
    if not has_launched_marker(instance, AWS_PYTEST_LAUNCHED_TAG):
        return None
    return parse_iso_utc(parse_tag_value(instance.get("tags", ()), AWS_CREATED_AT_TAG_KEY))


def cleanup_old_aws_test_instances(client: VpsReaperClient, max_age: timedelta, now: datetime) -> int:
    """Destroy pytest-launched EC2 instances older than ``max_age``; return the count cleaned up.

    Thin wrapper over the shared reaper with AWS's creation-tag extractor. Surfaces failures: a
    scan error propagates and a non-404 destroy failure raises ``VpsLeakCleanupError``.
    """
    return cleanup_old_test_instances(client, aws_test_created_at, max_age, now)
