from datetime import datetime
from datetime import timezone

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr_smolvm.errors import SmolvmHostRenameError
from imbue.mngr_smolvm.host_store import HostRecord
from imbue.mngr_smolvm.host_store import SmolvmMachineConfig
from imbue.mngr_smolvm.instance import SmolvmProviderInstance
from imbue.mngr_smolvm.instance import _parse_build_args


def test_provider_capabilities(smolvm_provider: SmolvmProviderInstance) -> None:
    assert smolvm_provider.supports_snapshots is False
    assert smolvm_provider.supports_shutdown_hosts is True
    assert smolvm_provider.supports_volumes is True
    assert smolvm_provider.supports_mutable_tags is True


def test_snapshot_methods_raise(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()

    with pytest.raises(SnapshotsNotSupportedError):
        smolvm_provider.create_snapshot(host_id, SnapshotName("test"))

    assert smolvm_provider.list_snapshots(host_id) == []

    with pytest.raises(SnapshotsNotSupportedError):
        smolvm_provider.delete_snapshot(host_id, SnapshotId("snap-1"))


def test_rename_host_raises(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    with pytest.raises(SmolvmHostRenameError):
        smolvm_provider.rename_host(host_id, HostName("new-name"))


def test_tags_crud(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()

    # Initially empty
    assert smolvm_provider.get_host_tags(host_id) == {}

    # Set tags
    smolvm_provider.set_host_tags(host_id, {"env": "test", "team": "infra"})
    assert smolvm_provider.get_host_tags(host_id) == {"env": "test", "team": "infra"}

    # Add tags
    smolvm_provider.add_tags_to_host(host_id, {"version": "1.0"})
    tags = smolvm_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "team": "infra", "version": "1.0"}

    # Remove tags
    smolvm_provider.remove_tags_from_host(host_id, ["team"])
    tags = smolvm_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "version": "1.0"}


def test_volume_dir_creation(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    volume_dir = smolvm_provider._ensure_host_volume_dir(host_id)
    assert volume_dir.exists()
    assert volume_dir.is_dir()


def test_list_volumes_empty(smolvm_provider: SmolvmProviderInstance) -> None:
    assert smolvm_provider.list_volumes() == []


def test_get_volume_for_nonexistent_host(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    assert smolvm_provider.get_volume_for_host(host_id) is None


def test_get_volume_for_existing_host(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    smolvm_provider._ensure_host_volume_dir(host_id)
    volume = smolvm_provider.get_volume_for_host(host_id)
    assert volume is not None


def test_get_volume_for_host_returns_none_for_btrfs_mode_record(smolvm_provider: SmolvmProviderInstance) -> None:
    """When the host record locks in is_host_data_volume_exposed=False,
    get_volume_for_host returns None even if a stray host-side volume dir
    exists."""
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
        config=SmolvmMachineConfig(
            machine_name="mngr-btrfs-host",
            ssh_host_port=2222,
            is_host_data_volume_exposed=False,
            data_disk_spec="size=100,target=/mngr",
        ),
    )
    smolvm_provider._host_store.write_host_record(record)
    smolvm_provider._ensure_host_volume_dir(host_id)
    assert smolvm_provider.get_volume_for_host(host_id) is None


def test_parse_build_args_empty() -> None:
    parsed = _parse_build_args(())
    assert parsed.image_archive is None
    assert parsed.from_pack is None


def test_parse_build_args_image_archive() -> None:
    parsed = _parse_build_args(("--image-archive", "/tmp/img.tar"))
    assert parsed.image_archive is not None
    assert str(parsed.image_archive) == "/tmp/img.tar"
    assert parsed.from_pack is None


def test_parse_build_args_from_pack() -> None:
    parsed = _parse_build_args(("--from", "/tmp/x.smolmachine"))
    assert parsed.from_pack is not None
    assert str(parsed.from_pack) == "/tmp/x.smolmachine"


def test_parse_build_args_dockerfile_inline_form() -> None:
    parsed = _parse_build_args(("--dockerfile=Dockerfile",))
    assert parsed.dockerfile is not None
    assert str(parsed.dockerfile) == "Dockerfile"


def test_parse_build_args_rejects_both_sources() -> None:
    with pytest.raises(MngrError, match="mutually exclusive"):
        _parse_build_args(("--image-archive", "/tmp/img.tar", "--from", "/tmp/x.smolmachine"))


def test_parse_build_args_rejects_unknown_arg() -> None:
    with pytest.raises(MngrError, match="Unsupported smolvm build arg"):
        _parse_build_args(("--bogus",))


def test_parse_build_args_rejects_missing_value() -> None:
    with pytest.raises(MngrError, match="requires a PATH"):
        _parse_build_args(("--image-archive",))


def test_parse_build_args_rejects_empty_inline_value() -> None:
    with pytest.raises(MngrError, match="requires a PATH"):
        _parse_build_args(("--dockerfile=",))


def test_reset_caches(smolvm_provider: SmolvmProviderInstance) -> None:
    # Should not raise
    smolvm_provider.reset_caches()


def test_provider_dir_structure(smolvm_provider: SmolvmProviderInstance) -> None:
    assert "smolvm-test" in str(smolvm_provider._provider_dir)
    assert "providers" in str(smolvm_provider._provider_dir)
    assert "smolvm" in str(smolvm_provider._provider_dir)


def test_ensure_host_keypair_creates_and_is_idempotent(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    private_key_path, public_key_path = smolvm_provider._host_keypair_paths(host_id)
    assert not private_key_path.exists()

    private_pem, public_openssh = smolvm_provider._ensure_host_keypair(host_id)
    assert "PRIVATE KEY" in private_pem
    assert public_openssh.startswith("ssh-ed25519 ")
    assert private_key_path.exists()
    assert public_key_path.exists()

    # Idempotent: a second call returns the same key
    private_pem_2, public_openssh_2 = smolvm_provider._ensure_host_keypair(host_id)
    assert private_pem_2 == private_pem
    assert public_openssh_2 == public_openssh


def test_host_record_roundtrip_via_store(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="roundtrip-host",
            user_tags={"a": "b"},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        ssh_hostname="127.0.0.1",
        ssh_port=43210,
        ssh_user="root",
        config=SmolvmMachineConfig(
            machine_name="mngr-roundtrip-host",
            ssh_host_port=43210,
            image="alpine:3.19",
        ),
    )
    smolvm_provider._host_store.write_host_record(record)
    loaded = smolvm_provider._host_store.read_host_record(host_id, use_cache=False)
    assert loaded is not None
    assert loaded.config is not None
    assert loaded.config.machine_name == "mngr-roundtrip-host"
    assert loaded.config.ssh_host_port == 43210
    assert loaded.ssh_user == "root"
