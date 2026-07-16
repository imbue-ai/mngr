"""Vultr adapter for the shared, age-based VPS test-instance reaper.

The reaper logic lives in ``imbue.mngr_vps.leak_cleanup`` (shared across every VPS provider).
This module supplies only Vultr's plumbing: the ``mngr-vultr-test-created=<timestamp>`` tag that
``conftest.py`` stamps on every test VPS (via ``MNGR_VPS_EXTRA_TAGS``) and the matching
``CreatedAtExtractor`` that reads it back. The tag's presence marks an instance as test-created
(so production VPSes, which never carry it, are never reaped) and its value gives the age.

Driven by ``scripts/cleanup_old_vultr_test_instances.py`` (run by a CI job on every push to main
and pull request), analogous to Modal's ``cleanup_old_modal_test_environments``.
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
# Timestamp format embedded in the created tag. No colons -> safe as a Vultr tag string; mirrors
# Modal's TEST_ENV format. ``build_test_created_tag`` writes it and the extractor below parses it.
VULTR_TEST_CREATED_TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d-%H-%M-%S"


def vultr_test_created_at(instance: Mapping[str, Any]) -> datetime | None:
    """Return a test VPS's UTC creation time, or ``None`` to leave it alone.

    Vultr has no separate ``mngr-pytest-launched`` marker (unlike AWS/GCP/Azure): the presence of
    the ``mngr-vultr-test-created`` tag is itself the test marker, so a production VPS (which never
    carries it) is never reaped.
    """
    raw = parse_tag_value(instance.get("tags", ()), VULTR_TEST_CREATED_TAG_KEY)
    return parse_strptime_utc(raw, VULTR_TEST_CREATED_TIMESTAMP_FORMAT)


def build_test_created_tag(now: datetime) -> str:
    """Build the ``mngr-vultr-test-created=<timestamp>`` tag for a VPS created at ``now`` (UTC)."""
    return f"{VULTR_TEST_CREATED_TAG_KEY}={now.strftime(VULTR_TEST_CREATED_TIMESTAMP_FORMAT)}"


def cleanup_old_vultr_test_instances(client: VpsReaperClient, max_age: timedelta, now: datetime) -> int:
    """Destroy test-created Vultr instances older than ``max_age``; return the count cleaned up.

    Thin wrapper over the shared reaper with Vultr's creation-tag extractor. Surfaces failures:
    a scan error propagates and a non-404 destroy failure raises ``VpsLeakCleanupError`` (see
    ``imbue.mngr_vps.leak_cleanup``).
    """
    return cleanup_old_test_instances(client, vultr_test_created_at, max_age, now)
