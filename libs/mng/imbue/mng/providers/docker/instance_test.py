import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.docker.instance import CONTAINER_SSH_PORT
from imbue.mng.providers.docker.instance import DockerProviderInstance
from imbue.mng.providers.docker.instance import LABEL_HOST_ID
from imbue.mng.providers.docker.instance import LABEL_HOST_NAME
from imbue.mng.providers.docker.instance import LABEL_PROVIDER
from imbue.mng.providers.docker.instance import LABEL_TAGS
from imbue.mng.providers.docker.instance import _get_ssh_host_from_docker_config
from imbue.mng.providers.docker.instance import build_container_labels
from imbue.mng.providers.docker.instance import parse_container_labels
from imbue.mng.providers.docker.testing import make_docker_provider
from imbue.mng.providers.docker.testing import make_docker_provider_with_local_volume

HOST_ID_A = "host-00000000000000000000000000000001"
HOST_ID_B = "host-00000000000000000000000000000002"


# =========================================================================
# Capability Properties
# =========================================================================


def test_docker_provider_name(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx, "my-docker")
    assert provider.name == ProviderInstanceName("my-docker")


def test_docker_provider_supports_snapshots(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    assert provider.supports_snapshots is True


def test_docker_provider_supports_shutdown_hosts(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    assert provider.supports_shutdown_hosts is True


def test_docker_provider_supports_volumes(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    assert provider.supports_volumes is True


def test_docker_provider_does_not_support_mutable_tags(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    assert provider.supports_mutable_tags is False


# =========================================================================
# Container Label Helpers
# =========================================================================


def test_build_container_labels_with_no_tags() -> None:
    labels = build_container_labels(
        host_id=HostId(HOST_ID_A),
        name=HostName("test-host"),
        provider_name="docker",
    )
    assert labels[LABEL_HOST_ID] == HOST_ID_A
    assert labels[LABEL_HOST_NAME] == "test-host"
    assert labels[LABEL_PROVIDER] == "docker"
    assert json.loads(labels[LABEL_TAGS]) == {}


def test_build_container_labels_with_tags() -> None:
    labels = build_container_labels(
        host_id=HostId(HOST_ID_A),
        name=HostName("test-host"),
        provider_name="docker",
        user_tags={"env": "test", "team": "infra"},
    )
    assert json.loads(labels[LABEL_TAGS]) == {"env": "test", "team": "infra"}


def test_parse_container_labels_extracts_host_id_and_name() -> None:
    labels = {
        LABEL_HOST_ID: HOST_ID_A,
        LABEL_HOST_NAME: "my-host",
        LABEL_PROVIDER: "docker",
        LABEL_TAGS: "{}",
    }
    host_id, name, provider, tags = parse_container_labels(labels)
    assert host_id == HostId(HOST_ID_A)
    assert name == HostName("my-host")
    assert provider == "docker"


def test_parse_container_labels_extracts_tags() -> None:
    labels = {
        LABEL_HOST_ID: HOST_ID_A,
        LABEL_HOST_NAME: "my-host",
        LABEL_PROVIDER: "docker",
        LABEL_TAGS: '{"env": "prod", "version": "2"}',
    }
    _, _, _, tags = parse_container_labels(labels)
    assert tags == {"env": "prod", "version": "2"}


def test_build_and_parse_container_labels_roundtrip() -> None:
    host_id = HostId(HOST_ID_B)
    name = HostName("roundtrip-host")
    provider = "my-docker-provider"
    user_tags = {"key1": "val1", "key2": "val2"}

    labels = build_container_labels(host_id, name, provider, user_tags)
    parsed_host_id, parsed_name, parsed_provider, parsed_tags = parse_container_labels(labels)

    assert parsed_host_id == host_id
    assert parsed_name == name
    assert parsed_provider == provider
    assert parsed_tags == user_tags


def test_parse_container_labels_handles_missing_tags_label() -> None:
    labels = {
        LABEL_HOST_ID: HOST_ID_A,
        LABEL_HOST_NAME: "my-host",
        LABEL_PROVIDER: "docker",
    }
    _, _, _, tags = parse_container_labels(labels)
    assert tags == {}


def test_parse_container_labels_handles_invalid_tags_json() -> None:
    labels = {
        LABEL_HOST_ID: HOST_ID_A,
        LABEL_HOST_NAME: "my-host",
        LABEL_PROVIDER: "docker",
        LABEL_TAGS: "not valid json {{{",
    }
    _, _, _, tags = parse_container_labels(labels)
    assert tags == {}


# =========================================================================
# SSH Host Resolution
# =========================================================================


def test_get_ssh_host_local_docker_empty_string() -> None:
    assert _get_ssh_host_from_docker_config("") == "127.0.0.1"


def test_get_ssh_host_local_docker_unix_socket() -> None:
    assert _get_ssh_host_from_docker_config("unix:///var/run/docker.sock") == "127.0.0.1"


def test_get_ssh_host_remote_docker_ssh() -> None:
    assert _get_ssh_host_from_docker_config("ssh://user@myserver") == "myserver"


def test_get_ssh_host_remote_docker_tcp() -> None:
    assert _get_ssh_host_from_docker_config("tcp://192.168.1.100:2376") == "192.168.1.100"


# =========================================================================
# Docker Run Command Building
# =========================================================================


def test_build_docker_run_command_includes_mandatory_flags(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test-container",
        labels={"com.imbue.mng.host-id": HOST_ID_A},
        start_args=(),
    )
    assert "run" in cmd
    assert "-d" in cmd
    assert "--name" in cmd
    assert "test-container" in cmd
    assert f":{CONTAINER_SSH_PORT}" in cmd
    assert "debian:bookworm-slim" in cmd


def test_build_docker_run_command_includes_labels(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test",
        labels={"key1": "val1", "key2": "val2"},
        start_args=(),
    )
    assert "--label" in cmd
    label_indices = [i for i, arg in enumerate(cmd) if arg == "--label"]
    label_values = [cmd[i + 1] for i in label_indices]
    assert "key1=val1" in label_values
    assert "key2=val2" in label_values


def test_build_docker_run_command_passes_through_start_args(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test",
        labels={},
        start_args=("--cpus=2", "--memory=4g", "--gpus=all"),
    )
    assert "--cpus=2" in cmd
    assert "--memory=4g" in cmd
    assert "--gpus=all" in cmd


def test_build_docker_run_command_entrypoint_at_end(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    cmd = provider._build_docker_run_command(
        image="my-image",
        container_name="test",
        labels={},
        start_args=(),
    )
    # Image and entrypoint should be at the end: --entrypoint sh <image> -c <cmd>
    image_idx = cmd.index("my-image")
    assert cmd[image_idx - 1] == "sh"
    assert cmd[image_idx + 1] == "-c"


# =========================================================================
# Tag Methods (no Docker required)
# =========================================================================


def test_set_host_tags_raises_mng_error(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    with pytest.raises(MngError, match="does not support mutable tags"):
        provider.set_host_tags(HostId(HOST_ID_A), {"key": "val"})


def test_add_tags_to_host_raises_mng_error(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    with pytest.raises(MngError, match="does not support mutable tags"):
        provider.add_tags_to_host(HostId(HOST_ID_A), {"key": "val"})


def test_remove_tags_from_host_raises_mng_error(temp_mng_ctx: MngContext) -> None:
    provider = make_docker_provider(temp_mng_ctx)
    with pytest.raises(MngError, match="does not support mutable tags"):
        provider.remove_tags_from_host(HostId(HOST_ID_A), ["key"])


# =========================================================================
# Volume Methods
# =========================================================================


def test_list_volumes_returns_empty_when_no_volumes_dir(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    provider = make_docker_provider_with_local_volume(temp_mng_ctx, tmp_path)
    assert provider.list_volumes() == []


def test_list_volumes_discovers_vol_directories(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """list_volumes returns VolumeInfo for vol-* directories."""
    provider = make_docker_provider_with_local_volume(temp_mng_ctx, tmp_path)
    vol_id = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    (tmp_path / "volumes" / str(vol_id)).mkdir(parents=True)

    volumes = provider.list_volumes()
    assert len(volumes) == 1
    assert volumes[0].volume_id == vol_id
    assert volumes[0].host_id == HostId(HOST_ID_A)


def test_list_volumes_discovers_multiple(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """list_volumes returns all vol-* directories."""
    provider = make_docker_provider_with_local_volume(temp_mng_ctx, tmp_path)
    vol_a = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    vol_b = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_B))
    (tmp_path / "volumes" / str(vol_a)).mkdir(parents=True)
    (tmp_path / "volumes" / str(vol_b)).mkdir(parents=True)

    volumes = provider.list_volumes()
    assert len(volumes) == 2
    assert {v.volume_id for v in volumes} == {vol_a, vol_b}


def test_delete_volume_removes_directory(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> None:
    """delete_volume removes a volume directory."""
    provider = make_docker_provider_with_local_volume(temp_mng_ctx, tmp_path)
    vol_id = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    vol_dir = tmp_path / "volumes" / str(vol_id)
    vol_dir.mkdir(parents=True)

    provider.delete_volume(vol_id)
    assert not vol_dir.exists()


def test_volume_id_for_host_is_deterministic() -> None:
    """_volume_id_for_host returns the same VolumeId for the same HostId."""
    host_id = HostId(HOST_ID_A)
    assert DockerProviderInstance._volume_id_for_host(host_id) == DockerProviderInstance._volume_id_for_host(host_id)


def test_volume_id_for_host_differs_for_different_hosts() -> None:
    """_volume_id_for_host returns different VolumeIds for different HostIds."""
    id1 = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    id2 = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_B))
    assert id1 != id2


# =========================================================================
# Host Resources
# =========================================================================


def test_get_host_resources_returns_defaults(temp_mng_ctx: MngContext) -> None:
    """get_host_resources returns default values without needing a Docker daemon."""
    provider = make_docker_provider(temp_mng_ctx, "test-resources")
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    host_data = CertifiedHostData(host_id=str(host_id), host_name="resources-test", created_at=now, updated_at=now)

    offline_host = OfflineHost(
        id=host_id,
        certified_host_data=host_data,
        provider_instance=provider,
        mng_ctx=temp_mng_ctx,
        on_updated_host_data=lambda host_id, data: None,
    )

    resources = provider.get_host_resources(offline_host)
    assert resources.cpu.count == 1
    assert resources.memory_gb == 1.0
