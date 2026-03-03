"""Tests for the LocalProviderInstance."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mng.config.consts import PROFILES_DIRNAME
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import LocalHostNotDestroyableError
from imbue.mng.errors import LocalHostNotStoppableError
from imbue.mng.errors import MngError
from imbue.mng.errors import SnapshotsNotSupportedError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.volume import HostVolume
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import VolumeId
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.providers.local.volume import LocalVolume
from imbue.mng.utils.testing import make_local_provider


def test_local_provider_name(local_provider: LocalProviderInstance) -> None:
    assert local_provider.name == LOCAL_PROVIDER_NAME


def test_local_provider_does_not_support_snapshots(local_provider: LocalProviderInstance) -> None:
    assert local_provider.supports_snapshots is False


def test_local_provider_supports_mutable_tags(local_provider: LocalProviderInstance) -> None:
    assert local_provider.supports_mutable_tags is True


def test_create_host_returns_host_with_persistent_id(temp_host_dir: Path, temp_config: MngConfig) -> None:
    # Use the same profile_dir for both providers to test persistence
    profile_dir = temp_host_dir / PROFILES_DIRNAME / uuid4().hex
    provider1 = make_local_provider(temp_host_dir, temp_config, profile_dir=profile_dir)
    provider2 = make_local_provider(temp_host_dir, temp_config, profile_dir=profile_dir)

    host1 = provider1.create_host(HostName("localhost"))
    host2 = provider2.create_host(HostName("localhost"))

    assert host1.id == host2.id


def test_create_host_generates_new_id_for_different_dirs(tmp_path: Path, mng_test_prefix: str) -> None:
    tmpdir1 = tmp_path / "host1"
    tmpdir2 = tmp_path / "host2"
    tmpdir1.mkdir()
    tmpdir2.mkdir()

    config1 = MngConfig(default_host_dir=tmpdir1, prefix=mng_test_prefix)
    config2 = MngConfig(default_host_dir=tmpdir2, prefix=mng_test_prefix)
    provider1 = make_local_provider(tmpdir1, config1)
    provider2 = make_local_provider(tmpdir2, config2)

    host1 = provider1.create_host(HostName("localhost"))
    host2 = provider2.create_host(HostName("localhost"))

    assert host1.id != host2.id


def test_host_id_persists_across_provider_instances(temp_host_dir: Path, temp_config: MngConfig) -> None:
    # Use the same profile_dir for both providers to test persistence
    profile_dir = temp_host_dir / PROFILES_DIRNAME / uuid4().hex
    provider1 = make_local_provider(temp_host_dir, temp_config, profile_dir=profile_dir)
    host1 = provider1.create_host(HostName("localhost"))
    host_id = host1.id

    provider2 = make_local_provider(temp_host_dir, temp_config, profile_dir=profile_dir)
    host2 = provider2.create_host(HostName("localhost"))

    assert host2.id == host_id

    # host_id is stored globally in default_host_dir (not per-profile)
    # because it identifies the local machine, not a profile
    host_id_path = temp_host_dir / "host_id"
    assert host_id_path.exists()
    assert host_id_path.read_text().strip() == host_id


def test_stop_host_raises_error(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    with pytest.raises(LocalHostNotStoppableError):
        local_provider.stop_host(host)


def test_destroy_host_raises_error(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    with pytest.raises(LocalHostNotDestroyableError):
        local_provider.destroy_host(host)


def test_start_host_returns_host(local_provider: LocalProviderInstance) -> None:
    host1 = local_provider.create_host(HostName("localhost"))
    host2 = local_provider.start_host(host1)

    assert host2.id == host1.id


def test_get_host_by_id(local_provider: LocalProviderInstance) -> None:
    host1 = local_provider.create_host(HostName("localhost"))
    host2 = local_provider.get_host(host1.id)

    assert host2.id == host1.id


def test_get_host_by_name(local_provider: LocalProviderInstance) -> None:
    host1 = local_provider.create_host(HostName("localhost"))
    host2 = local_provider.get_host(HostName("localhost"))

    assert host2.id == host1.id


def test_get_host_with_wrong_name_raises_error(local_provider: LocalProviderInstance) -> None:
    with pytest.raises(HostNotFoundError):
        local_provider.get_host(HostName("not-localhost"))


def test_create_host_with_wrong_name_raises_error(local_provider: LocalProviderInstance) -> None:
    with pytest.raises(UserInputError, match="localhost"):
        local_provider.create_host(HostName("not-localhost"))


def test_get_host_with_wrong_id_raises_error(local_provider: LocalProviderInstance) -> None:
    wrong_id = HostId.generate()

    with pytest.raises(HostNotFoundError) as exc_info:
        local_provider.get_host(wrong_id)

    assert exc_info.value.host == wrong_id


def test_list_hosts_returns_single_host(local_provider: LocalProviderInstance) -> None:
    hosts = local_provider.list_hosts(cg=local_provider.mng_ctx.concurrency_group)
    assert len(hosts) == 1


def test_create_snapshot_raises_error(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    with pytest.raises(SnapshotsNotSupportedError) as exc_info:
        local_provider.create_snapshot(host)

    assert exc_info.value.provider_name == LOCAL_PROVIDER_NAME


def test_list_snapshots_returns_empty_list(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    snapshots = local_provider.list_snapshots(host)
    assert snapshots == []


def test_delete_snapshot_raises_error(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    with pytest.raises(SnapshotsNotSupportedError):
        local_provider.delete_snapshot(host, SnapshotId("snap-test"))


def test_get_host_tags_empty_by_default(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    tags = local_provider.get_host_tags(host)
    assert tags == {}


def test_set_host_tags(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    tags = {"env": "test", "team": "backend"}

    local_provider.set_host_tags(host, tags)

    retrieved_tags = local_provider.get_host_tags(host)
    assert len(retrieved_tags) == 2
    assert retrieved_tags["env"] == "test"
    assert retrieved_tags["team"] == "backend"


def test_add_tags_to_host(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    local_provider.set_host_tags(host, {"env": "test"})

    local_provider.add_tags_to_host(host, {"team": "backend"})

    tags = local_provider.get_host_tags(host)
    assert len(tags) == 2
    assert tags["env"] == "test"
    assert tags["team"] == "backend"


def test_add_tags_updates_existing_tag(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    local_provider.set_host_tags(host, {"env": "test"})

    local_provider.add_tags_to_host(host, {"env": "prod"})

    tags = local_provider.get_host_tags(host)
    assert len(tags) == 1
    assert tags["env"] == "prod"


def test_remove_tags_from_host(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    local_provider.set_host_tags(host, {"env": "test", "team": "backend"})

    local_provider.remove_tags_from_host(host, ["env"])

    tags = local_provider.get_host_tags(host)
    assert len(tags) == 1
    assert tags["team"] == "backend"


def test_tags_persist_to_file(temp_host_dir: Path, temp_config: MngConfig) -> None:
    profile_dir = temp_host_dir / PROFILES_DIRNAME / uuid4().hex
    provider = make_local_provider(temp_host_dir, temp_config, profile_dir=profile_dir)
    host = provider.create_host(HostName("localhost"))

    provider.set_host_tags(host, {"env": "test"})

    # Tags are stored in default_host_dir (not per-profile) since they're local machine data
    labels_path = temp_host_dir / "providers" / "local" / "labels.json"
    assert labels_path.exists()

    with open(labels_path) as f:
        data = json.load(f)

    assert len(data) == 1
    assert data[0]["key"] == "env"
    assert data[0]["value"] == "test"


def test_create_host_with_tags(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"), tags={"env": "test"})

    retrieved_tags = local_provider.get_host_tags(host)
    assert len(retrieved_tags) == 1
    assert retrieved_tags["env"] == "test"


def test_rename_host_returns_host_with_same_id(local_provider: LocalProviderInstance) -> None:
    host1 = local_provider.create_host(HostName("localhost"))
    host2 = local_provider.rename_host(host1, HostName("new_name"))

    assert host2.id == host1.id


def test_get_connector_returns_pyinfra_host(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    connector = local_provider.get_connector(host)

    assert connector.name == "@local"


def test_get_host_resources_returns_valid_resources(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    resources = local_provider.get_host_resources(host)

    assert resources.cpu.count >= 1
    assert resources.memory_gb >= 0


def test_host_has_local_connector(local_provider: LocalProviderInstance) -> None:
    host = local_provider.create_host(HostName("localhost"))
    assert host.connector.connector_cls_name == "LocalConnector"


def test_list_volumes_returns_empty_for_fresh_setup(local_provider: LocalProviderInstance) -> None:
    """Local provider returns no volumes when no hosts/ subdirectory exists."""
    volumes = local_provider.list_volumes()
    assert len(volumes) == 0


def test_supports_volumes(local_provider: LocalProviderInstance) -> None:
    assert local_provider.supports_volumes is True


def test_get_volume_for_host_returns_host_volume(local_provider: LocalProviderInstance) -> None:
    """get_volume_for_host returns a HostVolume wrapping a LocalVolume."""
    host = local_provider.create_host(HostName("localhost"))
    host_volume = local_provider.get_volume_for_host(host)
    assert host_volume is not None
    assert isinstance(host_volume, HostVolume)
    assert isinstance(host_volume.volume, LocalVolume)


def test_get_volume_for_host_data_persists(local_provider: LocalProviderInstance) -> None:
    """Data written to a local volume persists across get_volume_for_host calls."""
    host = local_provider.create_host(HostName("localhost"))
    host_volume = local_provider.get_volume_for_host(host)
    assert host_volume is not None
    host_volume.volume.write_files({"test.txt": b"hello"})

    # Get volume again and verify data persists
    host_volume_2 = local_provider.get_volume_for_host(host)
    assert host_volume_2 is not None
    assert host_volume_2.volume.read_file("test.txt") == b"hello"


def test_list_volumes_finds_legacy_host_directories(local_provider: LocalProviderInstance) -> None:
    """list_volumes discovers host directories under hosts/."""
    # Simulate legacy data by creating a directory under hosts/
    hosts_dir = local_provider.mng_ctx.config.default_host_dir.expanduser() / "hosts"
    legacy_dir = hosts_dir / "host-abc123"
    legacy_dir.mkdir(parents=True)

    volumes = local_provider.list_volumes()
    assert len(volumes) == 1
    assert volumes[0].name == "host-abc123"


def test_delete_volume_removes_directory(local_provider: LocalProviderInstance) -> None:
    """delete_volume removes a volume directory under hosts/."""
    # Simulate legacy data by creating a directory under hosts/
    hosts_dir = local_provider.mng_ctx.config.default_host_dir.expanduser() / "hosts"
    legacy_dir = hosts_dir / "host-abc123"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "test.txt").write_text("data")

    # Verify volume directory exists
    volumes_before = local_provider.list_volumes()
    assert len(volumes_before) == 1

    # Delete using the volume_id from list_volumes
    local_provider.delete_volume(volumes_before[0].volume_id)

    # Verify it's gone
    volumes_after = local_provider.list_volumes()
    assert len(volumes_after) == 0


def test_delete_volume_raises_when_not_found(local_provider: LocalProviderInstance) -> None:
    """delete_volume raises MngError for nonexistent volume."""
    with pytest.raises(MngError):
        local_provider.delete_volume(VolumeId.generate())


def test_get_host_tags_returns_empty_when_labels_file_is_empty(temp_host_dir: Path, temp_config: MngConfig) -> None:
    """get_host_tags should return empty dict when labels file exists but is empty."""
    profile_dir = temp_host_dir / PROFILES_DIRNAME / uuid4().hex
    provider = make_local_provider(temp_host_dir, temp_config, profile_dir=profile_dir)
    host = provider.create_host(HostName("localhost"))

    labels_path = temp_host_dir / "providers" / "local" / "labels.json"
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text("")

    tags = provider.get_host_tags(host)
    assert tags == {}


# =============================================================================
# Tests for get_max_destroyed_host_persisted_seconds (BaseProviderInstance)
# =============================================================================


def test_get_max_destroyed_host_persisted_seconds_uses_global_default(
    local_provider: LocalProviderInstance,
) -> None:
    """When no provider-level override exists, uses the global config default."""
    result = local_provider.get_max_destroyed_host_persisted_seconds()
    assert result == local_provider.mng_ctx.config.default_destroyed_host_persisted_seconds


def test_get_max_destroyed_host_persisted_seconds_uses_provider_override(
    temp_host_dir: Path,
    mng_test_prefix: str,
) -> None:
    """When a provider-level override exists, uses that instead of the global default."""
    provider_name = ProviderInstanceName("local")
    provider_seconds = 86400.0
    config = MngConfig(
        default_host_dir=temp_host_dir,
        prefix=mng_test_prefix,
        providers={
            provider_name: ProviderInstanceConfig(
                backend=ProviderBackendName("local"),
                destroyed_host_persisted_seconds=provider_seconds,
            ),
        },
    )
    provider = make_local_provider(temp_host_dir, config, name=str(provider_name))

    result = provider.get_max_destroyed_host_persisted_seconds()
    assert result == provider_seconds


def test_get_max_destroyed_host_persisted_seconds_uses_custom_global_default(
    temp_host_dir: Path,
    mng_test_prefix: str,
) -> None:
    """When the global default is customized and no provider override exists, uses the global default."""
    custom_global_seconds = 172800.0
    config = MngConfig(
        default_host_dir=temp_host_dir,
        prefix=mng_test_prefix,
        default_destroyed_host_persisted_seconds=custom_global_seconds,
    )
    provider = make_local_provider(temp_host_dir, config)

    result = provider.get_max_destroyed_host_persisted_seconds()
    assert result == custom_global_seconds


def test_get_max_destroyed_host_persisted_seconds_provider_override_takes_precedence(
    temp_host_dir: Path,
    mng_test_prefix: str,
) -> None:
    """Provider-level setting takes precedence over the global default."""
    provider_name = ProviderInstanceName("local")
    global_seconds = 604800.0
    provider_seconds = 3600.0
    config = MngConfig(
        default_host_dir=temp_host_dir,
        prefix=mng_test_prefix,
        default_destroyed_host_persisted_seconds=global_seconds,
        providers={
            provider_name: ProviderInstanceConfig(
                backend=ProviderBackendName("local"),
                destroyed_host_persisted_seconds=provider_seconds,
            ),
        },
    )
    provider = make_local_provider(temp_host_dir, config, name=str(provider_name))

    result = provider.get_max_destroyed_host_persisted_seconds()
    assert result == provider_seconds
