from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResizeRequest
from imbue.mngr.interfaces.data_types import HostResizeValue
from imbue.mngr.interfaces.data_types import HostResourceLimits
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.constants import DEFAULT_LIMA_CPU_COUNT
from imbue.mngr_lima.constants import DEFAULT_LIMA_MEMORY_GIB
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


def test_rename_host_raises_when_record_missing(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    with pytest.raises(HostNotFoundError):
        lima_provider.rename_host(host_id, HostName("new-name"))


def test_rename_host_updates_persisted_host_name(lima_provider: LimaProviderInstance) -> None:
    """Renaming a Lima host rewrites the host name on its record (the instance name is untouched)."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="old-name",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        config=LimaHostConfig(
            instance_name=f"mngr-{host_id}",
            is_host_data_volume_exposed=False,
            host_data_disk_name="mngr-abc-data",
        ),
    )
    lima_provider._host_store.write_host_record(record)

    lima_provider.rename_host(host_id, HostName("new-name"))

    updated = lima_provider._host_store.read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.host_name == "new-name"
    # The limactl instance name is unchanged (no VM rename).
    assert updated.config is not None
    assert updated.config.instance_name == f"mngr-{host_id}"


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


def _write_resize_test_record(
    lima_provider: LimaProviderInstance,
    host_id: HostId,
    resources: HostResources | None,
) -> None:
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="resize-host",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        config=LimaHostConfig(instance_name=f"mngr-{host_id}"),
        resources=resources,
    )
    lima_provider._host_store.write_host_record(record)


def test_resize_host_persists_values_and_reports_configured(lima_provider: LimaProviderInstance) -> None:
    """A resize persists the new values on the record and reports them as configured.

    The instance does not exist in limactl, so the actual side is absent -- the
    caller sees only the durable configured values, exactly as for a stopped VM.
    """
    host_id = HostId.generate()
    _write_resize_test_record(
        lima_provider,
        host_id,
        HostResources(cpu=CpuResources(count=4), memory_gb=4.0, disk_gb=100.0, gpu=None),
    )

    report = lima_provider.resize_host(
        host_id,
        HostResizeRequest(cpu_count=HostResizeValue(value=6), memory_gib=HostResizeValue(value=8)),
    )

    assert report.configured == HostResourceLimits(cpu_count=6.0, memory_gib=8.0)
    assert report.actual is None
    updated = lima_provider._host_store.read_host_record(host_id, use_cache=False)
    assert updated is not None and updated.resources is not None
    assert updated.resources.cpu == CpuResources(count=6)
    assert updated.resources.memory_gb == 8.0
    # Untouched dimensions of the record survive the resize.
    assert updated.resources.disk_gb == 100.0


def test_resize_host_merges_partial_request_with_current_values(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    _write_resize_test_record(
        lima_provider,
        host_id,
        HostResources(cpu=CpuResources(count=2), memory_gb=6.0, disk_gb=100.0, gpu=None),
    )

    report = lima_provider.resize_host(host_id, HostResizeRequest(memory_gib=HostResizeValue(value=12)))

    assert report.configured == HostResourceLimits(cpu_count=2.0, memory_gib=12.0)
    updated = lima_provider._host_store.read_host_record(host_id, use_cache=False)
    assert updated is not None and updated.resources is not None
    assert updated.resources.cpu == CpuResources(count=2)
    assert updated.resources.memory_gb == 12.0


def test_resize_host_fills_defaults_for_legacy_record_without_resources(
    lima_provider: LimaProviderInstance,
) -> None:
    """A record written before resize support has no resources; the lima defaults fill the gap."""
    host_id = HostId.generate()
    _write_resize_test_record(lima_provider, host_id, None)

    report = lima_provider.resize_host(host_id, HostResizeRequest(cpu_count=HostResizeValue(value=6)))

    assert report.configured == HostResourceLimits(cpu_count=6.0, memory_gib=DEFAULT_LIMA_MEMORY_GIB)


def test_resize_host_rejects_clear_to_unlimited(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    _write_resize_test_record(
        lima_provider,
        host_id,
        HostResources(cpu=CpuResources(count=4), memory_gb=4.0, disk_gb=100.0, gpu=None),
    )

    with pytest.raises(UserInputError):
        lima_provider.resize_host(host_id, HostResizeRequest(memory_gib=HostResizeValue(value=None)))


def test_resize_host_raises_for_unknown_host(lima_provider: LimaProviderInstance) -> None:
    with pytest.raises(HostNotFoundError):
        lima_provider.resize_host(HostId.generate(), HostResizeRequest(cpu_count=HostResizeValue(value=2)))


def test_get_host_resource_limits_reports_defaults_for_legacy_record(
    lima_provider: LimaProviderInstance,
) -> None:
    host_id = HostId.generate()
    _write_resize_test_record(lima_provider, host_id, None)

    report = lima_provider.get_host_resource_limits(host_id)

    assert report.configured == HostResourceLimits(
        cpu_count=float(DEFAULT_LIMA_CPU_COUNT), memory_gib=DEFAULT_LIMA_MEMORY_GIB
    )
    assert report.actual is None


def test_get_resize_capabilities_reports_both_dimensions_with_physical_ceilings(
    lima_provider: LimaProviderInstance,
) -> None:
    capabilities = lima_provider.get_resize_capabilities()

    assert capabilities.is_resize_supported
    assert capabilities.cpu is not None and capabilities.memory_gib is not None
    # Ceilings come from the actual machine, so only their existence and sanity
    # (at least one CPU / one GiB) can be asserted portably.
    assert capabilities.cpu.ceiling is not None and capabilities.cpu.ceiling >= 1
    assert capabilities.memory_gib.ceiling is not None and capabilities.memory_gib.ceiling >= 1
