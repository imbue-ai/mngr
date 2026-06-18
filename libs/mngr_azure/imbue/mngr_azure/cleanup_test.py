"""Tests for the out-of-band Azure test-VM reaper."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from typing import Any

from azure.core.exceptions import AzureError

from imbue.mngr_azure.cleanup import AZURE_CREATED_AT_TAG_KEY
from imbue.mngr_azure.cleanup import cleanup_old_azure_test_instances
from imbue.mngr_azure.cleanup import find_old_test_vms
from imbue.mngr_azure.cleanup import force_delete_vms
from imbue.mngr_azure.cleanup import is_orphan_test_resource
from imbue.mngr_azure.cleanup import reclaim_orphan_test_network
from imbue.mngr_azure.client import AZURE_PYTEST_LAUNCHED_TAG
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
_RG = "mngr"


def _test_tags(created_at: datetime) -> dict[str, str]:
    return {AZURE_PYTEST_LAUNCHED_TAG: "true", AZURE_CREATED_AT_TAG_KEY: created_at.isoformat()}


def _vm(name: str, tags: dict[str, str]) -> Any:
    return SimpleNamespace(name=name, tags=tags)


def _nic(name: str, tags: dict[str, str], virtual_machine: Any = None) -> Any:
    return SimpleNamespace(name=name, tags=tags, virtual_machine=virtual_machine)


def _public_ip(name: str, tags: dict[str, str], ip_configuration: Any = None) -> Any:
    return SimpleNamespace(name=name, tags=tags, ip_configuration=ip_configuration)


def test_is_orphan_true_for_old_test_resource() -> None:
    assert is_orphan_test_resource(_vm("x", _test_tags(_NOW - timedelta(hours=3))), cutoff=_NOW)


def test_is_orphan_false_for_untagged_resource() -> None:
    # A production VM carries no pytest-launched tag and must never be reaped.
    assert not is_orphan_test_resource(_vm("prod", {AZURE_CREATED_AT_TAG_KEY: _NOW.isoformat()}), cutoff=_NOW)


def test_is_orphan_false_when_created_at_missing() -> None:
    assert not is_orphan_test_resource(_vm("x", {AZURE_PYTEST_LAUNCHED_TAG: "true"}), cutoff=_NOW)


def test_is_orphan_false_for_unparseable_created_at() -> None:
    tags = {AZURE_PYTEST_LAUNCHED_TAG: "true", AZURE_CREATED_AT_TAG_KEY: "not-a-timestamp"}
    assert not is_orphan_test_resource(_vm("x", tags), cutoff=_NOW)


def test_find_keeps_only_vms_older_than_max_age() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        _vm("old", _test_tags(_NOW - timedelta(hours=3))),
        _vm("fresh", _test_tags(_NOW - timedelta(minutes=10))),
        _vm("prod", {}),
    ]
    result = find_old_test_vms(compute, _RG, max_age=timedelta(hours=1), now=_NOW)
    assert result == ["old"]


def test_find_scan_error_returns_empty() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.list_error = AzureError("boom")
    assert find_old_test_vms(compute, _RG, max_age=timedelta(hours=1), now=_NOW) == []


def test_force_delete_swallows_errors() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.delete_error = AzureError("boom")
    # Must not raise even though the underlying delete fails.
    force_delete_vms(compute, _RG, ["x"])


def test_reclaim_deletes_unattached_old_network_only() -> None:
    network = FakeNetworkClient()
    network.network_interfaces.list_result = [
        _nic("old-nic", _test_tags(_NOW - timedelta(hours=3))),
        _nic("attached-nic", _test_tags(_NOW - timedelta(hours=3)), virtual_machine=SimpleNamespace(id="/vm/x")),
        _nic("fresh-nic", _test_tags(_NOW - timedelta(minutes=5))),
    ]
    network.public_ip_addresses.list_result = [
        _public_ip("old-ip", _test_tags(_NOW - timedelta(hours=3))),
        _public_ip("attached-ip", _test_tags(_NOW - timedelta(hours=3)), ip_configuration=SimpleNamespace(id="/cfg")),
    ]
    reclaim_orphan_test_network(network, _RG, max_age=timedelta(hours=1), now=_NOW)
    assert network.network_interfaces.deleted == ["old-nic"]
    assert network.public_ip_addresses.deleted == ["old-ip"]


def test_cleanup_deletes_old_vms_and_reclaims_network() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        _vm("old1", _test_tags(_NOW - timedelta(hours=5))),
        _vm("fresh", _test_tags(_NOW - timedelta(minutes=5))),
    ]
    network = FakeNetworkClient()
    network.network_interfaces.list_result = [_nic("old-nic", _test_tags(_NOW - timedelta(hours=3)))]
    cleaned = cleanup_old_azure_test_instances(compute, network, _RG, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 1
    assert compute.virtual_machines.deleted == ["old1"]
    assert network.network_interfaces.deleted == ["old-nic"]


def test_cleanup_returns_zero_when_nothing_old() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [_vm("fresh", _test_tags(_NOW - timedelta(minutes=5)))]
    network = FakeNetworkClient()
    cleaned = cleanup_old_azure_test_instances(compute, network, _RG, max_age=timedelta(hours=1), now=_NOW)
    assert cleaned == 0
    assert compute.virtual_machines.deleted == []
