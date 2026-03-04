"""Tests for the SSHProviderInstance."""

from pathlib import Path

import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import SnapshotsNotSupportedError
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import VolumeId
from imbue.mng.providers.ssh.instance import SSHHostConfig
from imbue.mng.providers.ssh.instance import SSHProviderInstance


def make_ssh_provider(
    temp_mng_ctx: MngContext,
    hosts: dict[str, SSHHostConfig] | None = None,
) -> SSHProviderInstance:
    """Create an SSHProviderInstance for testing."""
    if hosts is None:
        hosts = {
            "test-host": SSHHostConfig(address="localhost", port=22),
        }
    return SSHProviderInstance(
        name=ProviderInstanceName("ssh-test"),
        host_dir=Path("/tmp/mng"),
        mng_ctx=temp_mng_ctx,
        hosts=hosts,
    )


def test_ssh_provider_name(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    assert provider.name == ProviderInstanceName("ssh-test")


def test_ssh_provider_does_not_support_snapshots(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    assert provider.supports_snapshots is False


def test_ssh_provider_does_not_support_volumes(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    assert provider.supports_volumes is False


def test_ssh_provider_does_not_support_mutable_tags(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    assert provider.supports_mutable_tags is False


def test_create_snapshot_raises_error(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(SnapshotsNotSupportedError):
        provider.create_snapshot(HostId.generate())


def test_list_snapshots_returns_empty_list(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    snapshots = provider.list_snapshots(HostId.generate())
    assert snapshots == []


def test_delete_snapshot_raises_error(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(SnapshotsNotSupportedError):
        provider.delete_snapshot(HostId.generate(), SnapshotId("snap-test"))


def test_list_volumes_returns_empty_list(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    volumes = provider.list_volumes()
    assert volumes == []


def test_delete_volume_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.delete_volume(VolumeId.generate())


def test_get_host_tags_returns_empty_dict(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    tags = provider.get_host_tags(HostId.generate())
    assert tags == {}


def test_discover_hosts_returns_all_configured_hosts(temp_mng_ctx: MngContext) -> None:
    hosts = {
        "host1": SSHHostConfig(address="localhost", port=22),
        "host2": SSHHostConfig(address="localhost", port=2222),
    }
    provider = make_ssh_provider(temp_mng_ctx, hosts=hosts)
    listed_hosts = provider.discover_hosts(cg=provider.mng_ctx.concurrency_group)
    assert len(listed_hosts) == 2


def test_discover_hosts_returns_empty_when_no_hosts_configured(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx, hosts={})
    hosts = provider.discover_hosts(cg=provider.mng_ctx.concurrency_group)
    assert hosts == []


def test_get_host_by_name(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    host = provider.get_host(HostName("test-host"))
    assert host is not None


def test_get_host_not_found_for_unknown_id(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(HostNotFoundError):
        provider.get_host(HostId.generate())


def test_get_host_not_found_for_unknown_name(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(HostNotFoundError):
        provider.get_host(HostName("nonexistent"))


def test_create_host_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.create_host(HostName("test-host"))


def test_stop_host_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.stop_host(HostId.generate())


def test_start_host_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.start_host(HostId.generate())


def test_destroy_host_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.destroy_host(HostId.generate())


def test_set_host_tags_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.set_host_tags(HostId.generate(), {"key": "value"})


def test_add_tags_to_host_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.add_tags_to_host(HostId.generate(), {"key": "value"})


def test_remove_tags_from_host_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.remove_tags_from_host(HostId.generate(), ["key"])


def test_rename_host_raises_not_implemented(temp_mng_ctx: MngContext) -> None:
    provider = make_ssh_provider(temp_mng_ctx)
    with pytest.raises(NotImplementedError):
        provider.rename_host(HostId.generate(), HostName("new-name"))


def test_host_dir_is_set_correctly(temp_mng_ctx: MngContext) -> None:
    provider = SSHProviderInstance(
        name=ProviderInstanceName("ssh-test"),
        host_dir=Path("/custom/remote/path"),
        mng_ctx=temp_mng_ctx,
        hosts={},
    )
    assert provider.host_dir == Path("/custom/remote/path")


def test_get_host_resources_returns_defaults(temp_mng_ctx: MngContext) -> None:
    """get_host_resources should return sensible defaults."""
    provider = make_ssh_provider(temp_mng_ctx)

    # Create a mock host interface-like object with just an id
    class MockHost:
        id = HostId.generate()

    resources = provider.get_host_resources(MockHost())  # ty: ignore[invalid-argument-type]
    assert resources.cpu.count >= 1
    assert resources.memory_gb >= 0


def test_close_is_noop(temp_mng_ctx: MngContext) -> None:
    """close should be a no-op for SSH provider."""
    provider = make_ssh_provider(temp_mng_ctx)
    # Should not raise
    provider.close()


def test_host_id_is_deterministic(temp_mng_ctx: MngContext) -> None:
    """The same host name should always produce the same host ID."""
    provider = make_ssh_provider(temp_mng_ctx)

    host1 = provider.get_host(HostName("test-host"))
    host2 = provider.get_host(HostName("test-host"))

    assert host1.id == host2.id


def test_get_host_by_id_works(temp_mng_ctx: MngContext) -> None:
    """Should be able to get a host by its deterministic ID."""
    provider = make_ssh_provider(temp_mng_ctx)

    host_by_name = provider.get_host(HostName("test-host"))
    host_by_id = provider.get_host(host_by_name.id)

    assert host_by_id.id == host_by_name.id


def test_different_host_names_have_different_ids(temp_mng_ctx: MngContext) -> None:
    """Different host names should have different IDs."""
    hosts = {
        "host1": SSHHostConfig(address="localhost", port=22),
        "host2": SSHHostConfig(address="localhost", port=2222),
    }
    provider = make_ssh_provider(temp_mng_ctx, hosts=hosts)

    host1 = provider.get_host(HostName("host1"))
    host2 = provider.get_host(HostName("host2"))

    assert host1.id != host2.id
