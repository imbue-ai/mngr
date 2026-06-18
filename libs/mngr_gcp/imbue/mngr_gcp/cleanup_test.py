"""Tests for the GCP adapter of the shared test-instance reaper.

The reaper mechanics (age filter, destroy, surface-on-failure) are covered by
``libs/mngr_vps/imbue/mngr_vps/leak_cleanup_test.py``. These tests cover only GCP's plumbing: the
extractor requires the pytest-launched label (so production instances are never matched) and
reads the ISO ``mngr-created-at`` value from instance *metadata*, and the thin wrapper delegates
to the shared reaper.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from imbue.mngr_gcp.cleanup import cleanup_old_gcp_test_instances
from imbue.mngr_gcp.cleanup import gcp_test_created_at
from imbue.mngr_gcp.client import CREATED_AT_METADATA_KEY
from imbue.mngr_gcp.client import GCP_PYTEST_LAUNCHED_LABEL
from imbue.mngr_vps.primitives import VpsInstanceId

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _instance(instance_id: str, created_at: datetime | None, *, launched: bool = True) -> dict[str, Any]:
    tags = [f"{GCP_PYTEST_LAUNCHED_LABEL}=true"] if launched else []
    metadata = {CREATED_AT_METADATA_KEY: created_at.isoformat()} if created_at is not None else {}
    return {"id": instance_id, "tags": tags, "metadata": metadata}


class _FakeReaperClient:
    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self._instances = instances
        self.destroyed_ids: list[str] = []

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]:
        return self._instances

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        self.destroyed_ids.append(str(instance_id))


def test_extractor_reads_created_at_from_metadata() -> None:
    at = _NOW - timedelta(hours=3)
    assert gcp_test_created_at(_instance("t", at)) == at


def test_extractor_requires_pytest_launched_label() -> None:
    # Production instance: created-at present in metadata but no pytest-launched label.
    assert gcp_test_created_at(_instance("prod", _NOW - timedelta(days=9), launched=False)) is None


def test_extractor_returns_none_without_created_at_metadata() -> None:
    assert gcp_test_created_at(_instance("t", None)) is None


def test_cleanup_destroys_only_old_test_instances() -> None:
    client = _FakeReaperClient(
        [
            _instance("old", _NOW - timedelta(hours=5)),
            _instance("fresh", _NOW - timedelta(minutes=5)),
            _instance("prod", _NOW - timedelta(days=2), launched=False),
        ]
    )
    cleaned = cleanup_old_gcp_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 1
    assert client.destroyed_ids == ["old"]
