"""Tests for the SSHProviderInstance."""

from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.ssh.instance import SSHHostConfig
from imbue.mngr.providers.ssh.instance import SSHProviderInstance


def make_ssh_provider(
    temp_mngr_ctx: MngrContext,
    hosts: dict[str, SSHHostConfig] | None = None,
    dynamic_hosts_file: Path | None = None,
) -> SSHProviderInstance:
    """Create an SSHProviderInstance for testing."""
    if hosts is None:
        hosts = {
            "test-host": SSHHostConfig(address="localhost", port=22),
        }
    return SSHProviderInstance(
        name=ProviderInstanceName("ssh-test"),
        host_dir=Path("/tmp/mngr"),
        mngr_ctx=temp_mngr_ctx,
        hosts=hosts,
        dynamic_hosts_file=dynamic_hosts_file,
    )


def test_ssh_provider_name(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    assert provider.name == ProviderInstanceName("ssh-test")


def test_ssh_provider_does_not_support_snapshots(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    assert provider.supports_snapshots is False


def test_ssh_provider_does_not_support_volumes(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    assert provider.supports_volumes is False


def test_ssh_provider_does_not_support_mutable_tags(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    assert provider.supports_mutable_tags is False


def test_create_snapshot_raises_error(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(SnapshotsNotSupportedError):
        provider.create_snapshot(HostId.generate())


def test_list_snapshots_returns_empty_list(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    snapshots = provider.list_snapshots(HostId.generate())
    assert snapshots == []


def test_delete_snapshot_raises_error(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(SnapshotsNotSupportedError):
        provider.delete_snapshot(HostId.generate(), SnapshotId("snap-test"))


def test_list_volumes_returns_empty_list(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    volumes = provider.list_volumes()
    assert volumes == []


def test_delete_volume_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.delete_volume(VolumeId.generate())


def test_get_host_tags_returns_empty_dict(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    tags = provider.get_host_tags(HostId.generate())
    assert tags == {}


def test_discover_hosts_returns_all_configured_hosts(temp_mngr_ctx: MngrContext) -> None:
    hosts = {
        "host1": SSHHostConfig(address="localhost", port=22),
        "host2": SSHHostConfig(address="localhost", port=2222),
    }
    provider = make_ssh_provider(temp_mngr_ctx, hosts=hosts)
    listed_hosts = provider.discover_hosts(cg=provider.mngr_ctx.concurrency_group)
    assert len(listed_hosts) == 2


def test_discover_hosts_returns_empty_when_no_hosts_configured(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx, hosts={})
    hosts = provider.discover_hosts(cg=provider.mngr_ctx.concurrency_group)
    assert hosts == []


def test_get_host_by_name(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    host = provider.get_host(HostName("test-host"))
    assert host is not None


def test_get_host_not_found_for_unknown_id(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(HostNotFoundError):
        provider.get_host(HostId.generate())


def test_get_host_not_found_for_unknown_name(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(HostNotFoundError):
        provider.get_host(HostName("nonexistent"))


def test_create_host_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.create_host(HostName("test-host"))


def test_stop_host_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.stop_host(HostId.generate())


def test_start_host_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.start_host(HostId.generate())


def test_destroy_host_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.destroy_host(HostId.generate())


def test_set_host_tags_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.set_host_tags(HostId.generate(), {"key": "value"})


def test_add_tags_to_host_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.add_tags_to_host(HostId.generate(), {"key": "value"})


def test_remove_tags_from_host_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.remove_tags_from_host(HostId.generate(), ["key"])


def test_rename_host_raises_not_implemented(temp_mngr_ctx: MngrContext) -> None:
    provider = make_ssh_provider(temp_mngr_ctx)
    with pytest.raises(NotImplementedError):
        provider.rename_host(HostId.generate(), HostName("new-name"))


def test_host_dir_is_set_correctly(temp_mngr_ctx: MngrContext) -> None:
    provider = SSHProviderInstance(
        name=ProviderInstanceName("ssh-test"),
        host_dir=Path("/custom/remote/path"),
        mngr_ctx=temp_mngr_ctx,
        hosts={},
    )
    assert provider.host_dir == Path("/custom/remote/path")


def test_get_host_resources_returns_defaults(temp_mngr_ctx: MngrContext) -> None:
    """get_host_resources should return sensible defaults."""
    provider = make_ssh_provider(temp_mngr_ctx)

    # Create a mock host interface-like object with just an id
    class MockHost:
        id = HostId.generate()

    resources = provider.get_host_resources(MockHost())  # ty: ignore[invalid-argument-type]
    assert resources.cpu.count >= 1
    assert resources.memory_gb >= 0


def test_close_is_noop(temp_mngr_ctx: MngrContext) -> None:
    """close should be a no-op for SSH provider."""
    provider = make_ssh_provider(temp_mngr_ctx)
    # Should not raise
    provider.close()


def test_host_id_is_deterministic(temp_mngr_ctx: MngrContext) -> None:
    """The same host name should always produce the same host ID."""
    provider = make_ssh_provider(temp_mngr_ctx)

    host1 = provider.get_host(HostName("test-host"))
    host2 = provider.get_host(HostName("test-host"))

    assert host1.id == host2.id


def test_get_host_by_id_works(temp_mngr_ctx: MngrContext) -> None:
    """Should be able to get a host by its deterministic ID."""
    provider = make_ssh_provider(temp_mngr_ctx)

    host_by_name = provider.get_host(HostName("test-host"))
    host_by_id = provider.get_host(host_by_name.id)

    assert host_by_id.id == host_by_name.id


def test_different_host_names_have_different_ids(temp_mngr_ctx: MngrContext) -> None:
    """Different host names should have different IDs."""
    hosts = {
        "host1": SSHHostConfig(address="localhost", port=22),
        "host2": SSHHostConfig(address="localhost", port=2222),
    }
    provider = make_ssh_provider(temp_mngr_ctx, hosts=hosts)

    host1 = provider.get_host(HostName("host1"))
    host2 = provider.get_host(HostName("host2"))

    assert host1.id != host2.id


# =========================================================================
# Dynamic hosts tests
# =========================================================================


def test_discover_hosts_includes_dynamic_hosts_from_file(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Dynamic hosts from the TOML file appear in discovery results."""
    dynamic_file = tmp_path / "dynamic_hosts.toml"
    dynamic_file.write_text('[dynamic-host-1]\naddress = "10.0.0.1"\nport = 2222\nuser = "root"\n')
    provider = make_ssh_provider(
        temp_mngr_ctx,
        hosts={"static-host": SSHHostConfig(address="localhost", port=22)},
        dynamic_hosts_file=dynamic_file,
    )

    discovered = provider.discover_hosts(cg=provider.mngr_ctx.concurrency_group)
    discovered_names = {str(h.host_name) for h in discovered}

    assert "static-host" in discovered_names
    assert "dynamic-host-1" in discovered_names
    assert len(discovered) == 2


def test_discover_hosts_static_takes_precedence_over_dynamic(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When a name collision occurs, the static host config is used."""
    dynamic_file = tmp_path / "dynamic_hosts.toml"
    dynamic_file.write_text('[shared-name]\naddress = "10.0.0.99"\nport = 9999\nuser = "dynamic-user"\n')
    static_config = SSHHostConfig(address="192.168.1.1", port=22, user="static-user")
    provider = make_ssh_provider(
        temp_mngr_ctx,
        hosts={"shared-name": static_config},
        dynamic_hosts_file=dynamic_file,
    )

    host = provider.get_host(HostName("shared-name"))

    # The host should use the static config's address, not the dynamic one
    assert host is not None
    # Verify the connector was created with the static config by checking the
    # pyinfra host has the static address
    connector = provider.get_connector(host.id)
    assert connector.name == "192.168.1.1"


def test_discover_hosts_ignores_missing_dynamic_file(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """No crash when the dynamic hosts file does not exist."""
    nonexistent_file = tmp_path / "does_not_exist.toml"
    provider = make_ssh_provider(
        temp_mngr_ctx,
        hosts={"static-host": SSHHostConfig(address="localhost", port=22)},
        dynamic_hosts_file=nonexistent_file,
    )

    discovered = provider.discover_hosts(cg=provider.mngr_ctx.concurrency_group)

    assert len(discovered) == 1
    assert str(discovered[0].host_name) == "static-host"


@pytest.mark.allow_warnings(match=r"Failed to parse dynamic hosts file")
def test_discover_hosts_ignores_malformed_dynamic_file(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Graceful handling of a malformed TOML file -- returns only static hosts."""
    dynamic_file = tmp_path / "dynamic_hosts.toml"
    dynamic_file.write_text("this is not valid toml [[[")
    provider = make_ssh_provider(
        temp_mngr_ctx,
        hosts={"static-host": SSHHostConfig(address="localhost", port=22)},
        dynamic_hosts_file=dynamic_file,
    )

    discovered = provider.discover_hosts(cg=provider.mngr_ctx.concurrency_group)

    assert len(discovered) == 1
    assert str(discovered[0].host_name) == "static-host"


def test_get_host_finds_dynamic_host(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """get_host resolves a host defined only in the dynamic hosts file."""
    dynamic_file = tmp_path / "dynamic_hosts.toml"
    dynamic_file.write_text('[leased-host]\naddress = "203.0.113.10"\nport = 2222\nuser = "root"\n')
    provider = make_ssh_provider(
        temp_mngr_ctx,
        hosts={},
        dynamic_hosts_file=dynamic_file,
    )

    host = provider.get_host(HostName("leased-host"))

    assert host is not None
    assert host.id == provider._host_id_for_name("leased-host")
