"""Tests for the Azure adapter of the shared test-instance reaper.

The reaper mechanics (age filter, destroy, surface-on-failure) are covered by
``libs/mngr_vps/imbue/mngr_vps/leak_cleanup_test.py``, and the orphaned-network reclaim by
``AzureVpsClient.reclaim_orphaned_network_resources``'s own tests. These tests cover only Azure's
plumbing: the extractor requires the pytest-launched marker (so production VMs are never matched)
and reads the ``mngr-created-at`` tag, and the thin wrapper reclaims network then delegates VM
reaping to the shared reaper.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.cleanup import azure_test_created_at
from imbue.mngr_azure.cleanup import cleanup_old_azure_test_instances
from imbue.mngr_azure.client import AZURE_PYTEST_LAUNCHED_TAG
from imbue.mngr_vps.primitives import VpsInstanceId

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _instance(instance_id: str, tags: list[str]) -> dict[str, Any]:
    return {"id": instance_id, "tags": tags}


def _test_tags(created_at: datetime) -> list[str]:
    return [f"{AZURE_PYTEST_LAUNCHED_TAG}=true", f"mngr-created-at={created_at.isoformat()}"]


class _FakeAzureReaperClient:
    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self._instances = instances
        self.destroyed_ids: list[str] = []
        self.reclaim_calls: list[ProviderInstanceName] = []

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]:
        return self._instances

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        self.destroyed_ids.append(str(instance_id))

    def reclaim_orphaned_network_resources(
        self, provider_name: ProviderInstanceName, dry_run: bool = False
    ) -> list[Any]:
        self.reclaim_calls.append(provider_name)
        return []


def test_extractor_requires_pytest_launched_marker() -> None:
    created = f"mngr-created-at={(_NOW - timedelta(hours=3)).isoformat()}"
    assert azure_test_created_at(_instance("prod", [created])) is None
    assert azure_test_created_at(
        _instance("test", [f"{AZURE_PYTEST_LAUNCHED_TAG}=true", created])
    ) == _NOW - timedelta(hours=3)


def test_cleanup_reclaims_network_and_destroys_only_old_vms() -> None:
    client = _FakeAzureReaperClient(
        [
            _instance("old", _test_tags(_NOW - timedelta(hours=5))),
            _instance("fresh", _test_tags(_NOW - timedelta(minutes=5))),
            _instance("prod", ["mngr-provider=azure"]),
        ]
    )
    cleaned = cleanup_old_azure_test_instances(client, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 1
    assert client.destroyed_ids == ["old"]
    # The orphaned-network reclaim always runs (even when no VM is old).
    assert len(client.reclaim_calls) == 1
