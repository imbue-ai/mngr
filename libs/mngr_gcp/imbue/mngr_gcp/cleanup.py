"""GCP adapter for the shared, age-based VPS test-instance reaper.

Supplies GCP's ``CreatedAtExtractor`` (reads the ``mngr-created-at`` metadata value) and a
thin wrapper over the shared reaper in ``imbue.mngr_vps.leak_cleanup``. GCE labels cannot
hold an ISO timestamp, so the creation time is stamped in metadata rather than a label.
"""

from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from typing import Any

from imbue.mngr_gcp.client import CREATED_AT_METADATA_KEY
from imbue.mngr_gcp.client import GCP_PYTEST_LAUNCHED_LABEL
from imbue.mngr_vps.leak_cleanup import VpsReaperClient
from imbue.mngr_vps.leak_cleanup import cleanup_old_test_instances
from imbue.mngr_vps.leak_cleanup import parse_iso_utc


def gcp_test_created_at(instance: Mapping[str, Any]) -> datetime | None:
    """Return a pytest-launched GCE instance's UTC creation time, or ``None`` to leave it alone."""
    if f"{GCP_PYTEST_LAUNCHED_LABEL}=true" not in instance.get("tags", ()):
        return None
    metadata = instance.get("metadata", {})
    return parse_iso_utc(metadata.get(CREATED_AT_METADATA_KEY))


def cleanup_old_gcp_test_instances(client: VpsReaperClient, max_age: timedelta, now: datetime) -> int:
    """Destroy pytest-launched GCE instances older than ``max_age``; return the count cleaned up."""
    return cleanup_old_test_instances(client, gcp_test_created_at, max_age, now)
