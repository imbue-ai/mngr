"""Tests for the shared, age-based VPS test-instance reaper."""

from collections.abc import Iterable
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import pytest

from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.leak_cleanup import VpsLeakCleanupError
from imbue.mngr_vps.leak_cleanup import cleanup_old_test_instances
from imbue.mngr_vps.leak_cleanup import destroy_leaked_instances
from imbue.mngr_vps.leak_cleanup import find_old_test_instances
from imbue.mngr_vps.leak_cleanup import has_launched_marker
from imbue.mngr_vps.leak_cleanup import parse_iso_utc
from imbue.mngr_vps.leak_cleanup import parse_strptime_utc
from imbue.mngr_vps.leak_cleanup import parse_tag_value
from imbue.mngr_vps.primitives import VpsInstanceId

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
_VULTR_FORMAT = "%Y-%m-%d-%H-%M-%S"
_MARKER = "mngr-pytest-launched"


def _instance(instance_id: str, tags: list[str]) -> dict[str, Any]:
    return {"id": instance_id, "tags": tags}


def _extract(instance: Mapping[str, Any]) -> datetime | None:
    """A representative AWS/Azure-style extractor: require the marker, then parse an ISO tag."""
    if not has_launched_marker(instance, _MARKER):
        return None
    return parse_iso_utc(parse_tag_value(instance.get("tags", ()), "mngr-created-at"))


class _FakeReaperClient:
    """In-memory stand-in for the list/destroy slice of a VPS client.

    ``fail_ids`` raise a 500 on destroy (a real failure to surface); ``missing_ids`` raise a 404
    (already gone, counts as cleaned).
    """

    def __init__(
        self,
        instances: list[dict[str, Any]],
        fail_ids: Iterable[str] = (),
        missing_ids: Iterable[str] = (),
        list_error: Exception | None = None,
    ) -> None:
        self._instances = instances
        self._fail_ids = set(fail_ids)
        self._missing_ids = set(missing_ids)
        self._list_error = list_error
        self.destroyed_ids: list[str] = []

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]:
        if self._list_error is not None:
            raise self._list_error
        return self._instances

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        as_str = str(instance_id)
        if as_str in self._missing_ids:
            raise VpsApiError(404, "not found")
        if as_str in self._fail_ids:
            raise VpsApiError(500, "boom")
        self.destroyed_ids.append(as_str)


# --- parsing helpers --------------------------------------------------------------------------


def test_parse_tag_value_returns_value_or_none() -> None:
    tags = ["mngr-provider=aws", "mngr-created-at=2026-06-16T09:30:15+00:00"]
    assert parse_tag_value(tags, "mngr-created-at") == "2026-06-16T09:30:15+00:00"
    assert parse_tag_value(tags, "absent") is None


def test_parse_iso_utc_handles_naive_and_aware_and_garbage() -> None:
    assert parse_iso_utc("2026-06-16T09:30:15+00:00") == datetime(2026, 6, 16, 9, 30, 15, tzinfo=timezone.utc)
    # A naive timestamp is assumed UTC.
    assert parse_iso_utc("2026-06-16T09:30:15") == datetime(2026, 6, 16, 9, 30, 15, tzinfo=timezone.utc)
    assert parse_iso_utc("not-a-timestamp") is None
    assert parse_iso_utc(None) is None


def test_parse_strptime_utc_handles_format_and_garbage() -> None:
    assert parse_strptime_utc("2026-06-16-09-30-15", _VULTR_FORMAT) == datetime(
        2026, 6, 16, 9, 30, 15, tzinfo=timezone.utc
    )
    assert parse_strptime_utc("not-a-timestamp", _VULTR_FORMAT) is None
    assert parse_strptime_utc(None, _VULTR_FORMAT) is None


# --- has_launched_marker ----------------------------------------------------------------------


def test_has_launched_marker() -> None:
    assert has_launched_marker(_instance("t", [f"{_MARKER}=true", "mngr-provider=aws"]), _MARKER)
    # A production instance carries no marker.
    assert not has_launched_marker(_instance("prod", ["mngr-provider=aws"]), _MARKER)


# --- find_old_test_instances ------------------------------------------------------------------


def _test_tags(created_at: datetime) -> list[str]:
    return [f"{_MARKER}=true", f"mngr-created-at={created_at.isoformat()}"]


def test_find_keeps_only_instances_older_than_max_age() -> None:
    old = _instance("old", _test_tags(_NOW - timedelta(hours=3)))
    fresh = _instance("fresh", _test_tags(_NOW - timedelta(minutes=10)))
    result = find_old_test_instances([old, fresh], _extract, max_age=timedelta(hours=1), now=_NOW)
    assert [inst["id"] for inst in result] == ["old"]


def test_find_ignores_non_test_instances() -> None:
    production = _instance("prod", ["mngr-provider=aws", f"mngr-created-at={(_NOW - timedelta(days=9)).isoformat()}"])
    assert find_old_test_instances([production], _extract, max_age=timedelta(hours=1), now=_NOW) == []


def test_find_boundary_exactly_max_age_is_not_old() -> None:
    edge = _instance("edge", _test_tags(_NOW - timedelta(hours=1)))
    assert find_old_test_instances([edge], _extract, max_age=timedelta(hours=1), now=_NOW) == []


# --- destroy / cleanup ------------------------------------------------------------------------


def test_destroy_leaked_instances_404_counts_as_cleaned() -> None:
    client = _FakeReaperClient([_instance("gone", [])], missing_ids=["gone"])
    failed = destroy_leaked_instances(client, [_instance("gone", [])])
    assert failed == []


def test_destroy_leaked_instances_reports_real_failures() -> None:
    client = _FakeReaperClient([], fail_ids=["bad"])
    failed = destroy_leaked_instances(client, [_instance("good", []), _instance("bad", [])])
    # Both attempted; only the 500 is reported, and "good" still got destroyed.
    assert failed == ["bad"]
    assert client.destroyed_ids == ["good"]


def test_cleanup_destroys_only_old_test_instances() -> None:
    client = _FakeReaperClient(
        [
            _instance("old1", _test_tags(_NOW - timedelta(hours=5))),
            _instance("fresh", _test_tags(_NOW - timedelta(minutes=5))),
            _instance("prod", ["mngr-provider=aws"]),
            _instance("old2", _test_tags(_NOW - timedelta(days=2))),
        ]
    )
    cleaned = cleanup_old_test_instances(client, _extract, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 2
    assert sorted(client.destroyed_ids) == ["old1", "old2"]


def test_cleanup_returns_zero_when_nothing_old() -> None:
    client = _FakeReaperClient([_instance("fresh", _test_tags(_NOW - timedelta(minutes=5)))])
    assert cleanup_old_test_instances(client, _extract, max_age=timedelta(hours=1), now=_NOW) == 0
    assert client.destroyed_ids == []


def test_cleanup_surfaces_scan_failure() -> None:
    # A scan failure must propagate, not be swallowed into "nothing leaked".
    client = _FakeReaperClient([], list_error=VpsApiError(503, "unavailable"))
    with pytest.raises(VpsApiError):
        cleanup_old_test_instances(client, _extract, max_age=timedelta(hours=1), now=_NOW)


def test_cleanup_raises_on_destroy_failure_after_attempting_all() -> None:
    client = _FakeReaperClient(
        [
            _instance("old1", _test_tags(_NOW - timedelta(hours=5))),
            _instance("old2", _test_tags(_NOW - timedelta(hours=5))),
        ],
        fail_ids=["old1"],
    )
    with pytest.raises(VpsLeakCleanupError) as exc_info:
        cleanup_old_test_instances(client, _extract, max_age=timedelta(hours=1), now=_NOW)
    # old2 was still destroyed even though old1 failed.
    assert client.destroyed_ids == ["old2"]
    assert exc_info.value.failed_instance_ids == ("old1",)
