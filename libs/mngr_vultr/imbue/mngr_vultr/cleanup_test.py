"""Tests for the Vultr adapter of the shared test-instance reaper.

The reaper mechanics (age filter, destroy, surface-on-failure) are covered by
``libs/mngr_vps/imbue/mngr_vps/leak_cleanup_test.py``. These tests cover only Vultr's plumbing:
the created-tag round-trips with the extractor, production VPSes are never matched, and the thin
wrapper delegates to the shared reaper.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vultr.cleanup import build_test_created_tag
from imbue.mngr_vultr.cleanup import cleanup_old_vultr_test_instances
from imbue.mngr_vultr.cleanup import vultr_test_created_at

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _instance(instance_id: str, tags: list[str]) -> dict[str, Any]:
    return {"id": instance_id, "label": f"label-{instance_id}", "tags": tags}


class _FakeReaperClient:
    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self._instances = instances
        self.destroyed_ids: list[str] = []

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]:
        return self._instances

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        self.destroyed_ids.append(str(instance_id))


def test_extractor_round_trips_with_build_tag() -> None:
    at = datetime(2026, 6, 16, 9, 30, 15, tzinfo=timezone.utc)
    assert vultr_test_created_at(_instance("x", [build_test_created_tag(at)])) == at


def test_extractor_ignores_production_vps() -> None:
    # A production VPS carries no test-created tag and must never be reaped.
    assert vultr_test_created_at(_instance("prod", ["mngr-provider=vultr", "minds_env=staging"])) is None


def test_extractor_returns_none_for_unparseable_tag() -> None:
    assert vultr_test_created_at(_instance("x", ["mngr-vultr-test-created=not-a-timestamp"])) is None


def test_cleanup_destroys_only_old_test_instances() -> None:
    client = _FakeReaperClient(
        [
            _instance("old", [build_test_created_tag(_NOW - timedelta(hours=5))]),
            _instance("fresh", [build_test_created_tag(_NOW - timedelta(minutes=5))]),
            _instance("prod", ["mngr-provider=vultr"]),
        ]
    )
    cleaned = cleanup_old_vultr_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 1
    assert client.destroyed_ids == ["old"]
