"""Vultr adapter for the shared, age-based VPS test-instance reaper.

Supplies Vultr's ``CreatedAtExtractor`` (reads the ``mngr-vultr-test-created`` tag) and a
thin wrapper over the shared reaper in ``imbue.mngr_vps.leak_cleanup``. The tag's presence
marks an instance as test-created (production VPSes never carry it) and its value gives the age.
"""

from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Final

from imbue.mngr_vps.leak_cleanup import VpsReaperClient
from imbue.mngr_vps.leak_cleanup import cleanup_old_test_instances
from imbue.mngr_vps.leak_cleanup import parse_strptime_utc
from imbue.mngr_vps.leak_cleanup import parse_tag_value

# Tag key marking a VPS as test-created and carrying its UTC creation time.
VULTR_TEST_CREATED_TAG_KEY: Final[str] = "mngr-vultr-test-created"
# Colon-free timestamp format for the created tag (safe as a Vultr tag string).
VULTR_TEST_CREATED_TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d-%H-%M-%S"


def vultr_test_created_at(instance: Mapping[str, Any]) -> datetime | None:
    """Return a test VPS's UTC creation time, or ``None`` to leave it alone."""
    raw = parse_tag_value(instance.get("tags", ()), VULTR_TEST_CREATED_TAG_KEY)
    return parse_strptime_utc(raw, VULTR_TEST_CREATED_TIMESTAMP_FORMAT)


def build_test_created_tag(now: datetime) -> str:
    """Build the ``mngr-vultr-test-created=<timestamp>`` tag for a VPS created at ``now`` (UTC)."""
    return f"{VULTR_TEST_CREATED_TAG_KEY}={now.strftime(VULTR_TEST_CREATED_TIMESTAMP_FORMAT)}"


def cleanup_old_vultr_test_instances(client: VpsReaperClient, max_age: timedelta, now: datetime) -> int:
    """Destroy test-created Vultr instances older than ``max_age``; return the count cleaned up."""
    return cleanup_old_test_instances(client, vultr_test_created_at, max_age, now)
