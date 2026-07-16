"""Tests for the AWS adapter of the shared test-instance reaper.

The reaper mechanics (age filter, destroy, surface-on-failure) are covered by
``libs/mngr_vps/imbue/mngr_vps/leak_cleanup_test.py``. These tests cover only AWS's plumbing: the
created-at extractor requires the pytest-launched marker (so production instances are never
matched) and reads the ``mngr-created-at`` tag, and the thin wrapper delegates to the shared
reaper.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from imbue.mngr_aws.cleanup import aws_test_created_at
from imbue.mngr_aws.cleanup import cleanup_old_aws_test_instances
from imbue.mngr_aws.client import AWS_PYTEST_LAUNCHED_TAG
from imbue.mngr_vps.primitives import VpsInstanceId

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _instance(instance_id: str, tags: list[str]) -> dict[str, Any]:
    return {"id": instance_id, "tags": tags}


def _test_tags(created_at: datetime) -> list[str]:
    return [f"{AWS_PYTEST_LAUNCHED_TAG}=true", f"mngr-created-at={created_at.isoformat()}"]


class _FakeReaperClient:
    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self._instances = instances
        self.destroyed_ids: list[str] = []

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]:
        return self._instances

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        self.destroyed_ids.append(str(instance_id))


def test_extractor_requires_pytest_launched_marker() -> None:
    created = f"mngr-created-at={(_NOW - timedelta(hours=3)).isoformat()}"
    # Production instance: created-at present but no pytest-launched marker -> never reaped.
    assert aws_test_created_at(_instance("prod", [created])) is None
    assert aws_test_created_at(_instance("test", [f"{AWS_PYTEST_LAUNCHED_TAG}=true", created])) == _NOW - timedelta(
        hours=3
    )


def test_extractor_returns_none_without_created_at() -> None:
    assert aws_test_created_at(_instance("t", [f"{AWS_PYTEST_LAUNCHED_TAG}=true"])) is None


def test_cleanup_destroys_only_old_test_instances() -> None:
    client = _FakeReaperClient(
        [
            _instance("old", _test_tags(_NOW - timedelta(hours=5))),
            _instance("fresh", _test_tags(_NOW - timedelta(minutes=5))),
            _instance("prod", ["mngr-provider=aws"]),
        ]
    )
    cleaned = cleanup_old_aws_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 1
    assert client.destroyed_ids == ["old"]
