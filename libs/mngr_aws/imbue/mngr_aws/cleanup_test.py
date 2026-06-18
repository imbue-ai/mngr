"""Tests for the out-of-band AWS test-instance reaper."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from botocore.exceptions import BotoCoreError

from imbue.mngr_aws.cleanup import cleanup_old_aws_test_instances
from imbue.mngr_aws.cleanup import find_old_test_instances
from imbue.mngr_aws.cleanup import terminate_test_instances
from imbue.mngr_aws.client import AWS_PYTEST_LAUNCHED_TAG

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]], scan_error: Exception | None) -> None:
        self._pages = pages
        self._scan_error = scan_error
        self.last_filters: list[dict[str, Any]] | None = None

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        # boto3's paginate takes a PascalCase ``Filters`` kwarg; accept it via
        # **kwargs so the fake matches the real call without a lint exception.
        self.last_filters = kwargs.get("Filters")
        if self._scan_error is not None:
            raise self._scan_error
        return self._pages


class _FakeEc2:
    """In-memory stand-in for the describe/terminate slice of a boto3 EC2 client.

    Models the server-side tag filter as already applied: ``instances`` are the
    instances the real EC2 API would return for the pytest-launched tag filter,
    so the age filtering in ``find_old_test_instances`` is what's exercised.
    """

    def __init__(
        self,
        instances: list[tuple[str, datetime]],
        scan_error: Exception | None = None,
        terminate_error: Exception | None = None,
    ) -> None:
        self._instances = instances
        self._scan_error = scan_error
        self._terminate_error = terminate_error
        self.terminated: list[str] = []
        self.paginator = _FakePaginator(self._pages(), scan_error)

    def _pages(self) -> list[dict[str, Any]]:
        return [
            {"Reservations": [{"Instances": [{"InstanceId": iid, "LaunchTime": lt} for iid, lt in self._instances]}]}
        ]

    def get_paginator(self, operation_name: str) -> _FakePaginator:
        assert operation_name == "describe_instances"
        return self.paginator

    def terminate_instances(self, **kwargs: Any) -> None:
        # boto3's terminate_instances takes a PascalCase ``InstanceIds`` kwarg;
        # accept it via **kwargs so the fake matches the real call.
        if self._terminate_error is not None:
            raise self._terminate_error
        self.terminated.extend(kwargs["InstanceIds"])


def test_find_keeps_only_instances_older_than_max_age() -> None:
    ec2 = _FakeEc2([("old", _NOW - timedelta(hours=3)), ("fresh", _NOW - timedelta(minutes=10))])
    assert find_old_test_instances(ec2, max_age=timedelta(hours=1), now=_NOW) == ["old"]


def test_find_boundary_exactly_max_age_is_not_old() -> None:
    ec2 = _FakeEc2([("edge", _NOW - timedelta(hours=1))])
    assert find_old_test_instances(ec2, max_age=timedelta(hours=1), now=_NOW) == []


def test_find_filters_server_side_on_pytest_launched_tag() -> None:
    # The production-safety guarantee (never touch untagged production instances)
    # rests on the server-side tag filter, so assert the scan requests it.
    ec2 = _FakeEc2([("old", _NOW - timedelta(hours=3))])
    find_old_test_instances(ec2, max_age=timedelta(hours=1), now=_NOW)
    assert ec2.paginator.last_filters is not None
    assert {"Name": f"tag:{AWS_PYTEST_LAUNCHED_TAG}", "Values": ["true"]} in ec2.paginator.last_filters


def test_find_scan_error_returns_empty() -> None:
    ec2 = _FakeEc2([("old", _NOW - timedelta(hours=3))], scan_error=BotoCoreError())
    assert find_old_test_instances(ec2, max_age=timedelta(hours=1), now=_NOW) == []


def test_terminate_swallows_errors() -> None:
    ec2 = _FakeEc2([], terminate_error=BotoCoreError())
    # Must not raise even though the underlying terminate fails.
    terminate_test_instances(ec2, ["i-1"])


def test_terminate_noop_on_empty() -> None:
    ec2 = _FakeEc2([])
    terminate_test_instances(ec2, [])
    assert ec2.terminated == []


def test_cleanup_terminates_only_old_instances() -> None:
    ec2 = _FakeEc2(
        [
            ("old1", _NOW - timedelta(hours=5)),
            ("fresh", _NOW - timedelta(minutes=5)),
            ("old2", _NOW - timedelta(days=2)),
        ]
    )
    cleaned = cleanup_old_aws_test_instances(ec2, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 2
    assert sorted(ec2.terminated) == ["old1", "old2"]


def test_cleanup_returns_zero_when_nothing_old() -> None:
    ec2 = _FakeEc2([("fresh", _NOW - timedelta(minutes=5))])
    cleaned = cleanup_old_aws_test_instances(ec2, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 0
    assert ec2.terminated == []
