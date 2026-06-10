import hashlib
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import DockerBuildTimeoutError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostFileWriteInterface
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.docker.instance import CONTAINER_SSH_PORT
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.instance import LABEL_HOST_ID
from imbue.mngr.providers.docker.instance import LABEL_HOST_NAME
from imbue.mngr.providers.docker.instance import LABEL_PROVIDER
from imbue.mngr.providers.docker.instance import LABEL_TAGS
from imbue.mngr.providers.docker.instance import _get_docker_context_host
from imbue.mngr.providers.docker.instance import _get_ssh_host_from_docker_config
from imbue.mngr.providers.docker.instance import build_container_labels
from imbue.mngr.providers.docker.instance import parse_container_labels
from imbue.mngr.providers.docker.instance import verify_engine_version_supports_volume_subpath
from imbue.mngr.providers.docker.testing import make_docker_provider
from imbue.mngr.providers.docker.testing import make_docker_provider_with_local_volume
from imbue.mngr.providers.docker.testing import make_offline_docker_provider
from imbue.mngr.providers.docker.testing import write_fake_docker_context

HOST_ID_A = "host-00000000000000000000000000000001"
HOST_ID_B = "host-00000000000000000000000000000002"


# =========================================================================
# Capability Properties
# =========================================================================


def test_docker_provider_name(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx, "my-docker")
    assert provider.name == ProviderInstanceName("my-docker")


def test_docker_provider_supports_snapshots(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    assert provider.supports_snapshots is True


def test_docker_provider_supports_shutdown_hosts(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    assert provider.supports_shutdown_hosts is True


def test_docker_provider_supports_volumes(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    assert provider.supports_volumes is True


def test_docker_provider_does_not_support_mutable_tags(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
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


@pytest.mark.allow_warnings(match=r"^Invalid JSON in container tags label: not valid json \{\{\{")
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
# Docker Context Host Resolution
# =========================================================================


def test_get_docker_context_host_returns_host_for_non_default_context(fake_docker_config: Path) -> None:
    """Non-default context returns the context's Host URL."""
    write_fake_docker_context(fake_docker_config, "desktop-linux", "unix:///Users/x/.docker/run/docker.sock")
    assert _get_docker_context_host() == "unix:///Users/x/.docker/run/docker.sock"


def test_get_docker_context_host_returns_none_for_default_context(fake_docker_config: Path) -> None:
    """Default context returns None so docker.from_env() is used."""
    write_fake_docker_context(fake_docker_config, "default", "")
    assert _get_docker_context_host() is None


def test_get_docker_context_host_returns_none_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing Docker config returns None."""
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "nonexistent"))
    assert _get_docker_context_host() is None


def test_get_docker_context_host_returns_none_when_config_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed Docker config returns None."""
    config_dir = tmp_path / "docker-config-bad"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("not json")
    monkeypatch.setenv("DOCKER_CONFIG", str(config_dir))
    assert _get_docker_context_host() is None


def test_get_docker_context_host_returns_none_when_context_meta_corrupted(
    fake_docker_config: Path,
) -> None:
    """Corrupted context meta.json returns None (the Docker SDK raises bare Exception)."""
    # Write config pointing to a non-default context
    (fake_docker_config / "config.json").write_text('{"currentContext": "bad-ctx"}')
    # Create a corrupted meta.json for that context
    ctx_id = hashlib.sha256(b"bad-ctx").hexdigest()
    meta_dir = fake_docker_config / "contexts" / "meta" / ctx_id
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "meta.json").write_text("not json")
    assert _get_docker_context_host() is None


# =========================================================================
# Docker Run Command Building
# =========================================================================


def test_build_docker_run_command_includes_mandatory_flags(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test-container",
        labels={"com.imbue.mngr.host-id": HOST_ID_A},
        start_args=(),
    )
    assert "run" in cmd
    assert "-d" in cmd
    assert "--name" in cmd
    assert "test-container" in cmd
    assert f":{CONTAINER_SSH_PORT}" in cmd
    assert "debian:bookworm-slim" in cmd


def test_build_docker_run_command_includes_labels(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
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


def test_build_docker_run_command_passes_through_start_args(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test",
        labels={},
        start_args=("--cpus=2", "--memory=4g", "--gpus=all"),
    )
    assert "--cpus=2" in cmd
    assert "--memory=4g" in cmd
    assert "--gpus=all" in cmd


def test_build_docker_run_command_entrypoint_at_end(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
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


def _make_docker_provider_with_runtime(mngr_ctx: MngrContext, docker_runtime: str | None) -> DockerProviderInstance:
    config = DockerProviderConfig(isolate_host_volumes=False, docker_runtime=docker_runtime)
    return DockerProviderInstance(
        name=ProviderInstanceName("test-docker"),
        host_dir=Path("/mngr"),
        mngr_ctx=mngr_ctx,
        config=config,
    )


def test_build_docker_run_command_includes_runtime_when_configured(temp_mngr_ctx: MngrContext) -> None:
    provider = _make_docker_provider_with_runtime(temp_mngr_ctx, docker_runtime="runsc")
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test",
        labels={},
        start_args=(),
    )
    runtime_idx = cmd.index("--runtime")
    assert cmd[runtime_idx + 1] == "runsc"


def test_build_docker_run_command_omits_runtime_by_default(temp_mngr_ctx: MngrContext) -> None:
    provider = _make_docker_provider_with_runtime(temp_mngr_ctx, docker_runtime=None)
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test",
        labels={},
        start_args=(),
    )
    assert "--runtime" not in cmd


def test_build_docker_run_command_passes_through_volume_mount_args(temp_mngr_ctx: MngrContext) -> None:
    """`volume_mount_args` tokens are inserted verbatim into the docker run command."""
    provider = make_docker_provider(temp_mngr_ctx)
    cmd = provider._build_docker_run_command(
        image="my-image",
        container_name="test",
        labels={},
        start_args=(),
        volume_mount_args=["--mount", "type=volume,source=foo,target=/bar,volume-subpath=baz"],
    )
    mount_idx = cmd.index("--mount")
    assert cmd[mount_idx + 1] == "type=volume,source=foo,target=/bar,volume-subpath=baz"


# =========================================================================
# Volume Mount Argument Building
# =========================================================================


def test_build_volume_mount_args_legacy_shared_mode(temp_mngr_ctx: MngrContext) -> None:
    """Legacy mode emits `-v <vol>:/mngr-state:rw` regardless of host id."""
    provider = make_docker_provider(temp_mngr_ctx)
    args = provider._build_volume_mount_args(HostId(HOST_ID_A), is_isolated=False)
    assert args[0] == "-v"
    assert args[1].endswith(":/mngr-state:rw")


def test_build_volume_mount_args_isolated_mode(temp_mngr_ctx: MngrContext) -> None:
    """Isolated mode emits `--mount type=volume,...,volume-subpath=volumes/vol-<hex>`."""
    provider = make_docker_provider(temp_mngr_ctx)
    host_id = HostId(HOST_ID_A)
    args = provider._build_volume_mount_args(host_id, is_isolated=True)
    assert args[0] == "--mount"
    spec = args[1]
    assert spec.startswith("type=volume,")
    assert f"target={provider.host_dir}" in spec
    expected_volume_id = provider._volume_id_for_host(host_id)
    assert f"volume-subpath=volumes/{expected_volume_id}" in spec
    # The state volume name appears as the source.
    assert f"source={provider._state_volume_name}" in spec


def test_build_volume_mount_args_disabled_returns_empty(temp_mngr_ctx: MngrContext) -> None:
    """When is_host_volume_created is False, no mount args are emitted in either mode."""
    config = DockerProviderConfig(is_host_volume_created=False, isolate_host_volumes=False)
    provider = DockerProviderInstance(
        name=ProviderInstanceName("test-no-vol"),
        host_dir=Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=config,
    )
    assert provider._build_volume_mount_args(HostId(HOST_ID_A), is_isolated=False) == []


def test_host_volume_symlink_target_is_none_when_isolated(temp_mngr_ctx: MngrContext) -> None:
    """Isolated mode has no symlink (host_dir IS the mount)."""
    provider = make_docker_provider(temp_mngr_ctx)
    assert provider._get_host_volume_symlink_target(HostId(HOST_ID_A), is_isolated=True) is None


def test_host_volume_symlink_target_points_into_state_mount_when_shared(temp_mngr_ctx: MngrContext) -> None:
    """Shared mode emits the per-host path under /mngr-state for the install-script to symlink to."""
    provider = make_docker_provider(temp_mngr_ctx)
    target = provider._get_host_volume_symlink_target(HostId(HOST_ID_A), is_isolated=False)
    assert target is not None
    assert target.startswith("/mngr-state/volumes/vol-")


# =========================================================================
# Engine Version Preflight
# =========================================================================


@pytest.mark.parametrize("version", ["25.0.0", "25.0.3", "25.1.0", "26.0.0", "100.0.0"])
def test_engine_version_supports_volume_subpath_accepts_25_or_newer(version: str) -> None:
    verify_engine_version_supports_volume_subpath(version)


@pytest.mark.parametrize("version", ["24.0.7", "24.0.0", "23.0.5", "20.10.21", "1.0.0"])
def test_engine_version_supports_volume_subpath_rejects_older(version: str) -> None:
    with pytest.raises(MngrError, match="requires Docker Engine 25.0\\+"):
        verify_engine_version_supports_volume_subpath(version)


@pytest.mark.parametrize("version", ["not-a-version", "abc.def", ""])
def test_engine_version_supports_volume_subpath_rejects_unparseable(version: str) -> None:
    with pytest.raises(MngrError):
        verify_engine_version_supports_volume_subpath(version)


@pytest.mark.parametrize("version", ["25.0.0-rc.1", "25.0-rc.1", "25.0-beta.2"])
def test_engine_version_supports_volume_subpath_accepts_prerelease_suffix(version: str) -> None:
    """Pre-release suffixes on either the minor or patch component are tolerated.

    `25.0.0-rc.1` matches the realistic Docker pre-release format; the
    other entries cover the parser's robustness to suffixes appearing on
    the minor segment.
    """
    verify_engine_version_supports_volume_subpath(version)


# =========================================================================
# Tag Methods (no Docker required)
# =========================================================================


def test_set_host_tags_raises_mngr_error(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    with pytest.raises(MngrError, match="does not support mutable tags"):
        provider.set_host_tags(HostId(HOST_ID_A), {"key": "val"})


def test_add_tags_to_host_raises_mngr_error(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    with pytest.raises(MngrError, match="does not support mutable tags"):
        provider.add_tags_to_host(HostId(HOST_ID_A), {"key": "val"})


def test_remove_tags_from_host_raises_mngr_error(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    with pytest.raises(MngrError, match="does not support mutable tags"):
        provider.remove_tags_from_host(HostId(HOST_ID_A), ["key"])


# =========================================================================
# Volume Methods
# =========================================================================


def test_list_volumes_returns_empty_when_no_volumes_dir(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    assert provider.list_volumes() == []


def test_list_volumes_discovers_vol_directories(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """list_volumes returns VolumeInfo for vol-* directories."""
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    vol_id = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    (tmp_path / "volumes" / str(vol_id)).mkdir(parents=True)

    volumes = provider.list_volumes()
    assert len(volumes) == 1
    assert volumes[0].volume_id == vol_id
    assert volumes[0].host_id == HostId(HOST_ID_A)


def test_list_volumes_discovers_multiple(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """list_volumes returns all vol-* directories."""
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    vol_a = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    vol_b = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_B))
    (tmp_path / "volumes" / str(vol_a)).mkdir(parents=True)
    (tmp_path / "volumes" / str(vol_b)).mkdir(parents=True)

    volumes = provider.list_volumes()
    assert len(volumes) == 2
    assert {v.volume_id for v in volumes} == {vol_a, vol_b}


def test_delete_volume_removes_directory(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """delete_volume removes a volume directory."""
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    vol_id = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    vol_dir = tmp_path / "volumes" / str(vol_id)
    vol_dir.mkdir(parents=True)

    provider.delete_volume(vol_id)
    assert not vol_dir.exists()


def test_offline_host_from_record_is_readable(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """An offline host built from a host record reads files from its volume.

    The destroy / GC paths obtain a stopped host via ``get_host`` (and thus
    ``_create_host_from_host_record``), not ``to_offline_host``; both must yield
    a ``HostFileReadInterface`` so ``on_before_host_destroy`` can still preserve
    session files from the volume. This guards that the readability wrapping
    lives at the shared construction site, not only on ``to_offline_host``.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=HOST_ID_A,
            host_name="h",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    host = provider._create_host_from_host_record(record)
    assert isinstance(host, HostFileReadInterface)

    vol_id = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    agent_dir = tmp_path / "volumes" / str(vol_id) / "agents" / "agent-x"
    agent_dir.mkdir(parents=True)
    (agent_dir / "f.txt").write_text("hi")

    assert host.read_text_file(host.host_dir / "agents" / "agent-x" / "f.txt") == "hi"
    assert host.path_exists(host.host_dir / "agents" / "agent-x")
    assert not host.path_exists(host.host_dir / "agents" / "missing")


@pytest.mark.allow_warnings(match=r"File mode is not settable when writing to an offline host's volume")
def test_offline_host_from_record_is_writable(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """An offline host built from a host record writes files to its volume.

    Backs `mngr file put` against a stopped host: writing through the host's
    HostFileWriteInterface lands the bytes on the persisted volume (with --mode
    ignored), and the write is read back through the same host.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=HOST_ID_A,
            host_name="h",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    host = provider._create_host_from_host_record(record)
    assert isinstance(host, OfflineHostWithVolume)
    assert isinstance(host, HostFileWriteInterface)

    target = host.host_dir / "agents" / "agent-x" / "staged.txt"
    host.write_file(target, b"hello")

    vol_id = DockerProviderInstance._volume_id_for_host(HostId(HOST_ID_A))
    on_disk = tmp_path / "volumes" / str(vol_id) / "agents" / "agent-x" / "staged.txt"
    assert on_disk.read_bytes() == b"hello"
    assert host.read_file(target) == b"hello"

    # mode is not settable on a volume write -- it is ignored, not an error.
    host.write_file(target, b"world", mode="0644")
    assert host.read_file(target) == b"world"


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


def test_get_host_resources_returns_defaults(temp_mngr_ctx: MngrContext) -> None:
    """get_host_resources returns default values without needing a Docker daemon."""
    provider = make_docker_provider(temp_mngr_ctx, "test-resources")
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    host_data = CertifiedHostData(host_id=str(host_id), host_name="resources-test", created_at=now, updated_at=now)

    offline_host = OfflineHost(
        id=host_id,
        certified_host_data=host_data,
        provider_instance=provider,
        mngr_ctx=temp_mngr_ctx,
        on_updated_host_data=lambda host_id, data: None,
    )

    resources = provider.get_host_resources(offline_host)
    assert resources.cpu.count == 1
    assert resources.memory_gb == 1.0


# =========================================================================
# Docker Daemon Offline Behavior
# =========================================================================


@pytest.mark.docker_sdk
def test_docker_client_raises_provider_unavailable_when_daemon_offline(temp_mngr_ctx: MngrContext) -> None:
    """Accessing _docker_client when the daemon is unreachable raises ProviderUnavailableError."""
    provider = make_offline_docker_provider(temp_mngr_ctx)
    with pytest.raises(ProviderUnavailableError, match="not available"):
        _ = provider._docker_client


@pytest.mark.docker_sdk
def test_docker_client_error_is_mngr_error_subclass(temp_mngr_ctx: MngrContext) -> None:
    """ProviderUnavailableError is a MngrError, so existing except MngrError handlers catch it."""
    provider = make_offline_docker_provider(temp_mngr_ctx)
    with pytest.raises(MngrError):
        _ = provider._docker_client


@pytest.mark.docker_sdk
def test_discover_hosts_returns_empty_when_daemon_offline(temp_mngr_ctx: MngrContext) -> None:
    """discover_hosts gracefully returns [] when Docker is unreachable."""
    provider = make_offline_docker_provider(temp_mngr_ctx)
    result = provider.discover_hosts(cg=temp_mngr_ctx.concurrency_group)
    assert result == []


@pytest.mark.docker_sdk
def test_discover_hosts_and_agents_returns_empty_when_daemon_offline(temp_mngr_ctx: MngrContext) -> None:
    """discover_hosts_and_agents gracefully returns {} when Docker is unreachable."""
    provider = make_offline_docker_provider(temp_mngr_ctx)
    result = provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)
    assert result == {}


# =========================================================================
# Build Timeout
# =========================================================================


class _BuildTimingOutDockerProvider(DockerProviderInstance):
    """Provider subclass that simulates a build process timing out.

    Lets us exercise the timeout-translation path in `_build_image` without
    needing a real Docker daemon or a long-running subprocess.
    """

    def _run_docker_creation_command(
        self,
        args: list[str],
        timeout: float = 300,
        executable: DockerBuilder = DockerBuilder.DOCKER,
    ) -> FinishedProcess:
        raise ProcessTimeoutError(
            command=tuple([executable.value.lower()] + args),
            stdout="",
            stderr="",
            is_output_already_logged=True,
        )


def test_build_image_translates_process_timeout_to_docker_build_timeout_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A timed-out `docker build` surfaces as a clear DockerBuildTimeoutError."""
    config = DockerProviderConfig(build_timeout_seconds=42, isolate_host_volumes=False)
    provider = _BuildTimingOutDockerProvider(
        name=ProviderInstanceName("test-docker-timeout"),
        host_dir=Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=config,
    )
    with pytest.raises(DockerBuildTimeoutError) as exc_info:
        provider._build_image(["--file", "Dockerfile", "."], "test-tag")
    assert exc_info.value.timeout_seconds == 42
    assert exc_info.value.provider_name == ProviderInstanceName("test-docker-timeout")
    assert "timed out after 42 seconds" in str(exc_info.value)


def test_docker_build_timeout_error_help_text_mentions_config_setting() -> None:
    """The error tells the user how to raise the timeout in their config."""
    error = DockerBuildTimeoutError(
        provider_name=ProviderInstanceName("my-docker"),
        timeout_seconds=600,
    )
    assert error.user_help_text is not None
    assert "build_timeout_seconds" in error.user_help_text
    assert "my-docker" in error.user_help_text
