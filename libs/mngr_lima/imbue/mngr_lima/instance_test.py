from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.errors import LimaHostRenameError
from imbue.mngr_lima.host_store import HostRecord
from imbue.mngr_lima.host_store import LimaHostConfig
from imbue.mngr_lima.instance import LimaProviderInstance
from imbue.mngr_lima.instance import _parse_size_to_gb
from imbue.mngr_lima.limactl import LimaSshConfig


def test_provider_capabilities(lima_provider: LimaProviderInstance) -> None:
    assert lima_provider.supports_snapshots is False
    assert lima_provider.supports_shutdown_hosts is True
    assert lima_provider.supports_volumes is True
    assert lima_provider.supports_mutable_tags is True


def test_snapshot_methods_raise(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()

    with pytest.raises(SnapshotsNotSupportedError):
        lima_provider.create_snapshot(host_id, SnapshotName("test"))

    assert lima_provider.list_snapshots(host_id) == []

    with pytest.raises(SnapshotsNotSupportedError):
        lima_provider.delete_snapshot(host_id, SnapshotId("snap-1"))


def test_rename_host_raises(lima_provider: LimaProviderInstance) -> None:
    from imbue.mngr.primitives import HostName

    host_id = HostId.generate()
    with pytest.raises(LimaHostRenameError):
        lima_provider.rename_host(host_id, HostName("new-name"))


def test_tags_crud(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()

    # Initially empty
    assert lima_provider.get_host_tags(host_id) == {}

    # Set tags
    lima_provider.set_host_tags(host_id, {"env": "test", "team": "infra"})
    assert lima_provider.get_host_tags(host_id) == {"env": "test", "team": "infra"}

    # Add tags
    lima_provider.add_tags_to_host(host_id, {"version": "1.0"})
    tags = lima_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "team": "infra", "version": "1.0"}

    # Remove tags
    lima_provider.remove_tags_from_host(host_id, ["team"])
    tags = lima_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "version": "1.0"}


def test_volume_dir_creation(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    volume_dir = lima_provider._ensure_host_volume_dir(host_id)
    assert volume_dir.exists()
    assert volume_dir.is_dir()


def test_list_volumes_empty(lima_provider: LimaProviderInstance) -> None:
    assert lima_provider.list_volumes() == []


def test_get_volume_for_nonexistent_host(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    assert lima_provider.get_volume_for_host(host_id) is None


def test_get_volume_for_existing_host(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    lima_provider._ensure_host_volume_dir(host_id)
    volume = lima_provider.get_volume_for_host(host_id)
    assert volume is not None


def test_get_volume_for_host_returns_none_for_btrfs_mode_record(lima_provider: LimaProviderInstance) -> None:
    """When the host record locks in is_host_data_volume_exposed=False,
    get_volume_for_host returns None even if a stray host-side volume dir
    exists. Callers (events.py, mngr_claude on_before_host_destroy) already
    handle None by skipping or falling back to online-host SSH."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="btrfs-host",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        config=LimaHostConfig(
            instance_name="mngr-btrfs-host",
            is_host_data_volume_exposed=False,
            host_data_disk_name="mngr-abc-data",
        ),
    )
    lima_provider._host_store.write_host_record(record)
    # Even if a host-side volume dir is somehow present, the record's False
    # flag must short-circuit the result to None.
    lima_provider._ensure_host_volume_dir(host_id)
    assert lima_provider.get_volume_for_host(host_id) is None


def test_parse_size_to_gb() -> None:
    assert _parse_size_to_gb("4GiB") == 4.0
    assert _parse_size_to_gb("512MiB") == 0.5
    assert _parse_size_to_gb("1TiB") == 1024.0
    assert _parse_size_to_gb("8") == 8.0
    assert _parse_size_to_gb("invalid") == 4.0  # default fallback


def test_reset_caches(lima_provider: LimaProviderInstance) -> None:
    # Should not raise
    lima_provider.reset_caches()


def test_provider_dir_structure(lima_provider: LimaProviderInstance) -> None:
    # Verify the provider directory structure uses the provider name
    assert "lima-test" in str(lima_provider._provider_dir)
    assert "providers" in str(lima_provider._provider_dir)
    assert "lima" in str(lima_provider._provider_dir)


def test_ensure_host_keypair_creates_and_is_idempotent(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    private_key_path, public_key_path = lima_provider._host_keypair_paths(host_id)
    assert not private_key_path.exists()

    private_pem, public_openssh = lima_provider._ensure_host_keypair(host_id)
    assert "PRIVATE KEY" in private_pem
    assert public_openssh.startswith("ssh-ed25519 ")
    assert private_key_path.exists()
    assert public_key_path.exists()

    # A second call must load the existing keypair rather than regenerate it.
    private_pem_again, public_openssh_again = lima_provider._ensure_host_keypair(host_id)
    assert private_pem_again == private_pem
    assert public_openssh_again == public_openssh


def test_record_pre_injected_host_key_writes_known_hosts(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    _, public_openssh = lima_provider._ensure_host_keypair(host_id)

    lima_provider._record_pre_injected_host_key(host_id, "127.0.0.1", 60022)

    known_hosts = lima_provider._host_known_hosts_path(host_id).read_text()
    assert "[127.0.0.1]:60022" in known_hosts
    assert public_openssh.strip() in known_hosts


def test_record_pre_injected_host_key_rewrites_on_port_change(lima_provider: LimaProviderInstance) -> None:
    # Lima reassigns the forwarded port across restarts; the per-host known_hosts
    # file must reflect only the current port, with no stale entries from prior ports.
    host_id = HostId.generate()
    lima_provider._ensure_host_keypair(host_id)

    lima_provider._record_pre_injected_host_key(host_id, "127.0.0.1", 60022)
    lima_provider._record_pre_injected_host_key(host_id, "127.0.0.1", 60099)

    known_hosts = lima_provider._host_known_hosts_path(host_id).read_text()
    assert "[127.0.0.1]:60099" in known_hosts
    assert "[127.0.0.1]:60022" not in known_hosts
    assert known_hosts.count("\n") == 1


def test_effective_ssh_user_non_root_uses_lima_user(lima_provider: LimaProviderInstance) -> None:
    """The default (non-root) provider connects as Lima's own user and key."""
    ssh_config = LimaSshConfig(
        hostname="127.0.0.1", port=60022, user="josh", identity_file=Path("/home/josh/.lima/key")
    )
    user, identity = lima_provider._effective_ssh_user_and_identity(ssh_config, is_run_as_root=False)
    assert user == "josh"
    assert identity == Path("/home/josh/.lima/key")


def test_effective_ssh_user_root_uses_root_key(temp_mngr_ctx: MngrContext) -> None:
    """A run-as-root host connects as root using mngr's injected root client key."""
    config = LimaProviderConfig(
        host_dir=Path("/mngr"),
        is_host_data_volume_exposed=False,
        is_run_as_root=True,
        default_idle_timeout=60,
    )
    provider = LimaProviderInstance(
        name=ProviderInstanceName("lima-root-test"),
        host_dir=Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=config,
    )
    ssh_config = LimaSshConfig(
        hostname="127.0.0.1", port=60022, user="josh", identity_file=Path("/home/josh/.lima/key")
    )
    user, identity = provider._effective_ssh_user_and_identity(ssh_config, is_run_as_root=True)
    assert user == "root"
    # The injected root client key materializes under the provider's keys dir.
    assert identity.name == "root_ssh_key"
    assert identity.exists()


def test_delete_host_removes_keypair_dir(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    lima_provider._ensure_host_keypair(host_id)
    host_keys_dir = lima_provider._host_keys_dir(host_id)
    assert host_keys_dir.exists()

    now = datetime.now(timezone.utc)
    host_record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="test-host",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        )
    )
    lima_provider._host_store.write_host_record(host_record)

    lima_provider.delete_host(lima_provider._create_offline_host(host_record))
    assert not host_keys_dir.exists()
