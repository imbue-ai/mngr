"""GCP adapter for the shared, age-based VPS test-instance reaper.

The reaper logic lives in ``imbue.mngr_vps.leak_cleanup`` (shared across every VPS provider).
This module supplies only GCP's plumbing. Unlike AWS/Azure, GCP cannot stamp its creation time in
a label (GCE labels lowercase the value and forbid the colons in an ISO timestamp), so
``GcpVpsClient.create_instance`` writes ``mngr-created-at`` into instance *metadata* and only the
``mngr-pytest-launched`` marker into a label. ``list_instances()`` surfaces labels in ``tags`` and
metadata in ``metadata``, so the extractor reads the marker from one and the timestamp from the
other.

An instance is reapable only when it carries the pytest-launched label (so production instances
are never touched) and a parseable ``mngr-created-at`` metadata value. Driven by the session-end
hook in ``conftest.py`` and the standalone CI reaper ``scripts/cleanup_old_gcp_test_instances.py``.
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
    """Return a pytest-launched GCE instance's UTC creation time, or ``None`` to leave it alone.

    Requires the ``mngr-pytest-launched`` label (surfaced in ``tags``) so production instances are
    never matched, then reads the ISO ``mngr-created-at`` value from instance metadata.
    """
    if f"{GCP_PYTEST_LAUNCHED_LABEL}=true" not in instance.get("tags", ()):
        return None
    metadata = instance.get("metadata", {})
    return parse_iso_utc(metadata.get(CREATED_AT_METADATA_KEY))


def cleanup_old_gcp_test_instances(client: VpsReaperClient, max_age: timedelta, now: datetime) -> int:
    """Destroy pytest-launched GCE instances older than ``max_age``; return the count cleaned up.

    Thin wrapper over the shared reaper with GCP's metadata-based creation extractor. Surfaces
    failures: a scan error propagates and a non-404 destroy failure raises ``VpsLeakCleanupError``.
    """
    return cleanup_old_test_instances(client, gcp_test_created_at, max_age, now)
