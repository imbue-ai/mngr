"""Tests for the out-of-band GCP test-instance reaper."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from google.api_core import exceptions as google_api_exceptions
from google.cloud import compute_v1

from imbue.mngr_gcp.cleanup import cleanup_old_gcp_test_instances
from imbue.mngr_gcp.cleanup import find_old_test_instances
from imbue.mngr_gcp.cleanup import force_delete_instances
from imbue.mngr_gcp.client import GCP_PYTEST_LAUNCHED_LABEL
from imbue.mngr_gcp.testing import FakeInstancesClient

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
_PROJECT = "test-project"
_ZONE = "us-west1-a"


def _instance(name: str, created_at: datetime) -> compute_v1.Instance:
    return compute_v1.Instance(name=name, creation_timestamp=created_at.isoformat())


def test_find_keeps_only_instances_older_than_max_age() -> None:
    client = FakeInstancesClient()
    client.list_result = [
        _instance("old", _NOW - timedelta(hours=3)),
        _instance("fresh", _NOW - timedelta(minutes=10)),
    ]
    result = find_old_test_instances(client, _PROJECT, _ZONE, max_age=timedelta(hours=1), now=_NOW)
    assert result == ["old"]


def test_find_boundary_exactly_max_age_is_not_old() -> None:
    client = FakeInstancesClient()
    client.list_result = [_instance("edge", _NOW - timedelta(hours=1))]
    assert find_old_test_instances(client, _PROJECT, _ZONE, max_age=timedelta(hours=1), now=_NOW) == []


def test_find_filters_server_side_on_pytest_launched_label() -> None:
    # The production-safety guarantee rests on the server-side label filter, so
    # assert the scan requests it.
    client = FakeInstancesClient()
    find_old_test_instances(client, _PROJECT, _ZONE, max_age=timedelta(hours=1), now=_NOW)
    assert client.last_list_filter == f"labels.{GCP_PYTEST_LAUNCHED_LABEL}=true"


def test_find_skips_instance_with_unparseable_creation_timestamp() -> None:
    # An instance whose age cannot be established from creation_timestamp must
    # be left alone rather than crashing the scan (which runs in
    # pytest_sessionfinish), mirroring the AWS / Azure / Vultr reapers.
    client = FakeInstancesClient()
    client.list_result = [
        compute_v1.Instance(name="bad", creation_timestamp="not-a-timestamp"),
        _instance("old", _NOW - timedelta(hours=3)),
    ]
    assert find_old_test_instances(client, _PROJECT, _ZONE, max_age=timedelta(hours=1), now=_NOW) == ["old"]


def test_find_scan_error_returns_empty() -> None:
    client = FakeInstancesClient()
    client.list_error = google_api_exceptions.ServiceUnavailable("boom")
    assert find_old_test_instances(client, _PROJECT, _ZONE, max_age=timedelta(hours=1), now=_NOW) == []


def test_force_delete_swallows_errors() -> None:
    client = FakeInstancesClient()
    client.delete_error = google_api_exceptions.ServiceUnavailable("boom")
    # Must not raise even though the underlying delete fails.
    force_delete_instances(client, _PROJECT, _ZONE, ["x"])


def test_cleanup_deletes_only_old_instances() -> None:
    client = FakeInstancesClient()
    client.list_result = [
        _instance("old1", _NOW - timedelta(hours=5)),
        _instance("fresh", _NOW - timedelta(minutes=5)),
        _instance("old2", _NOW - timedelta(days=2)),
    ]
    cleaned = cleanup_old_gcp_test_instances(client, _PROJECT, _ZONE, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 2
    assert sorted(client.deleted) == ["old1", "old2"]


def test_cleanup_returns_zero_when_nothing_old() -> None:
    client = FakeInstancesClient()
    client.list_result = [_instance("fresh", _NOW - timedelta(minutes=5))]
    cleaned = cleanup_old_gcp_test_instances(client, _PROJECT, _ZONE, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 0
    assert client.deleted == []
