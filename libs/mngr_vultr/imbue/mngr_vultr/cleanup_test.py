"""Tests for the out-of-band Vultr test-instance reaper."""

from collections.abc import Iterable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vultr.cleanup import VULTR_TEST_CREATED_TAG_KEY
from imbue.mngr_vultr.cleanup import build_test_created_tag
from imbue.mngr_vultr.cleanup import cleanup_old_vultr_test_instances
from imbue.mngr_vultr.cleanup import find_old_test_instances
from imbue.mngr_vultr.cleanup import parse_test_created_at

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _instance(instance_id: str, tags: list[str]) -> dict[str, Any]:
    return {"id": instance_id, "label": f"label-{instance_id}", "tags": tags}


def _created_tag(at: datetime) -> str:
    return build_test_created_tag(at)


class _FakeReaperClient:
    """In-memory stand-in for the list/destroy slice of VultrVpsClient.

    ``fail_ids`` raise a 500 on destroy (real failure left for next run);
    ``missing_ids`` raise a 404 (already gone, counts as cleaned).
    """

    def __init__(
        self,
        instances: list[dict[str, Any]],
        fail_ids: Iterable[str] = (),
        missing_ids: Iterable[str] = (),
    ) -> None:
        self._instances = instances
        self._fail_ids = set(fail_ids)
        self._missing_ids = set(missing_ids)
        self.destroyed_ids: list[str] = []

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]:
        return self._instances

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        as_str = str(instance_id)
        if as_str in self._missing_ids:
            raise VpsApiError(404, "not found")
        if as_str in self._fail_ids:
            raise VpsApiError(500, "boom")
        self.destroyed_ids.append(as_str)


def test_parse_round_trips_with_build() -> None:
    at = datetime(2026, 6, 16, 9, 30, 15, tzinfo=timezone.utc)
    assert parse_test_created_at([build_test_created_tag(at)]) == at


def test_parse_returns_none_when_tag_absent() -> None:
    assert parse_test_created_at(["mngr-provider=vultr", "mngr-host-id=host-x"]) is None


def test_parse_returns_none_for_unparseable_timestamp() -> None:
    assert parse_test_created_at([f"{VULTR_TEST_CREATED_TAG_KEY}=not-a-timestamp"]) is None


def test_parse_picks_created_tag_among_others() -> None:
    at = datetime(2026, 6, 16, 1, 2, 3, tzinfo=timezone.utc)
    tags = ["mngr-provider=vultr", _created_tag(at), "mngr-vultr-test-session=abc123"]
    assert parse_test_created_at(tags) == at


def test_find_keeps_only_instances_older_than_max_age() -> None:
    old = _instance("old", [_created_tag(_NOW - timedelta(hours=3))])
    fresh = _instance("fresh", [_created_tag(_NOW - timedelta(minutes=10))])
    result = find_old_test_instances([old, fresh], max_age=timedelta(hours=1), now=_NOW)
    assert [inst["id"] for inst in result] == ["old"]


def test_find_ignores_non_test_instances() -> None:
    # A production VPS carries no test-created tag and must never be reaped,
    # even if it is ancient.
    production = _instance("prod", ["mngr-provider=vultr", "minds_env=staging"])
    assert find_old_test_instances([production], max_age=timedelta(hours=1), now=_NOW) == []


def test_find_boundary_exactly_max_age_is_not_old() -> None:
    at_cutoff = _instance("edge", [_created_tag(_NOW - timedelta(hours=1))])
    assert find_old_test_instances([at_cutoff], max_age=timedelta(hours=1), now=_NOW) == []


def test_cleanup_destroys_only_old_test_instances() -> None:
    client = _FakeReaperClient(
        [
            _instance("old1", [_created_tag(_NOW - timedelta(hours=5))]),
            _instance("fresh", [_created_tag(_NOW - timedelta(minutes=5))]),
            _instance("prod", ["mngr-provider=vultr"]),
            _instance("old2", [_created_tag(_NOW - timedelta(days=2))]),
        ]
    )
    cleaned = cleanup_old_vultr_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 2
    assert sorted(client.destroyed_ids) == ["old1", "old2"]


def test_cleanup_returns_zero_when_nothing_old() -> None:
    client = _FakeReaperClient([_instance("fresh", [_created_tag(_NOW - timedelta(minutes=5))])])
    cleaned = cleanup_old_vultr_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 0
    assert client.destroyed_ids == []


def test_cleanup_404_on_destroy_counts_as_cleaned() -> None:
    client = _FakeReaperClient(
        [_instance("old1", [_created_tag(_NOW - timedelta(hours=5))])],
        missing_ids=["old1"],
    )
    cleaned = cleanup_old_vultr_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 1


def test_cleanup_other_failure_does_not_count_and_does_not_raise() -> None:
    client = _FakeReaperClient(
        [
            _instance("old1", [_created_tag(_NOW - timedelta(hours=5))]),
            _instance("old2", [_created_tag(_NOW - timedelta(hours=5))]),
        ],
        fail_ids=["old1"],
    )
    # The 500 on old1 is logged and left for the next run; old2 still gets cleaned.
    cleaned = cleanup_old_vultr_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 1
    assert client.destroyed_ids == ["old2"]
