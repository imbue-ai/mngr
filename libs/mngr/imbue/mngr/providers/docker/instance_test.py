import hashlib
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import cast

import docker
import docker.errors
import docker.models.containers
import pytest
import requests
import requests.exceptions

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import DockerBuildTimeoutError
from imbue.mngr.errors import DockerRuntimeNotRegisteredError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostFileWriteInterface
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.docker.host_store import DockerHostStore
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.docker.instance import CONTAINER_SSH_PORT
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.instance import LABEL_HOST_ID
from imbue.mngr.providers.docker.instance import LABEL_HOST_NAME
from imbue.mngr.providers.docker.instance import LABEL_PROVIDER
from imbue.mngr.providers.docker.instance import LABEL_TAGS
from imbue.mngr.providers.docker.instance import _get_docker_context_host
from imbue.mngr.providers.docker.instance import _get_ssh_host_from_docker_config
from imbue.mngr.providers.docker.instance import _is_gvisor_runtime_rootfs_ephemeral
from imbue.mngr.providers.docker.instance import build_container_labels
from imbue.mngr.providers.docker.instance import parse_container_labels
from imbue.mngr.providers.docker.instance import verify_engine_version_supports_volume_subpath
from imbue.mngr.providers.docker.testing import make_docker_provider
from imbue.mngr.providers.docker.testing import make_docker_provider_with_local_volume
from imbue.mngr.providers.docker.testing import make_offline_docker_provider
from imbue.mngr.providers.docker.testing import write_fake_docker_context
from imbue.mngr.providers.docker.volume import STATE_CONTAINER_TYPE_LABEL
from imbue.mngr.providers.docker.volume import STATE_CONTAINER_TYPE_VALUE
from imbue.mngr.providers.local.volume import LocalVolume
from imbue.mngr.utils.testing import capture_loguru

HOST_ID_A = "host-00000000000000000000000000000001"
HOST_ID_B = "host-00000000000000000000000000000002"


class _FakeContainer:
    """Minimal stand-in for a docker SDK container: just the attributes
    ``_raise_if_state_container_stopped`` reads (``labels`` + ``status``)."""

    def __init__(self, labels: dict[str, str], status: str) -> None:
        self.labels = labels
        self.status = status


def _fake_containers(*containers: _FakeContainer) -> list[docker.models.containers.Container]:
    # The helper only reads ``.labels`` / ``.status``, so the duck-typed fakes
    # stand in for real SDK containers; cast to satisfy the static type.
    return cast(list[docker.models.containers.Container], list(containers))


def test_raise_if_state_container_stopped_raises_when_present_but_stopped(temp_mngr_ctx: MngrContext) -> None:
    # A stopped state container means host records are unreachable. Discovery
    # must treat this as ProviderUnavailableError (so the retain-on-error path
    # keeps last-known hosts) rather than silently reporting zero hosts.
    provider = make_docker_provider(temp_mngr_ctx)
    containers = _fake_containers(_FakeContainer({STATE_CONTAINER_TYPE_LABEL: STATE_CONTAINER_TYPE_VALUE}, "exited"))
    with pytest.raises(ProviderUnavailableError):
        provider._raise_if_state_container_stopped(containers)


def test_raise_if_state_container_stopped_ok_when_running(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    containers = _fake_containers(_FakeContainer({STATE_CONTAINER_TYPE_LABEL: STATE_CONTAINER_TYPE_VALUE}, "running"))
    # A running state container is the normal case -- must not raise.
    provider._raise_if_state_container_stopped(containers)


def test_raise_if_state_container_stopped_noop_when_absent(temp_mngr_ctx: MngrContext) -> None:
    # No state container in the list (e.g. a never-created / removed env): leave
    # the genuinely-empty case alone -- only present-but-stopped raises.
    provider = make_docker_provider(temp_mngr_ctx)
    provider._raise_if_state_container_stopped(_fake_containers(_FakeContainer({LABEL_HOST_ID: HOST_ID_A}, "running")))
    provider._raise_if_state_container_stopped(_fake_containers())


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


def _make_unknown_runtime_process_error(runtime: str) -> ProcessError:
    """Build a `ProcessError` mirroring Docker's unregistered-runtime failure."""
    return ProcessError(
        command=("docker", "run", "--runtime", runtime, "debian:bookworm-slim"),
        stdout="",
        stderr=f"docker: Error response from daemon: unknown or invalid runtime name: {runtime}\n",
        returncode=125,
    )


def test_runtime_not_registered_error_maps_unknown_runtime_process_error(temp_mngr_ctx: MngrContext) -> None:
    provider = _make_docker_provider_with_runtime(temp_mngr_ctx, docker_runtime="runsc")
    error = provider._runtime_not_registered_error_or_none(_make_unknown_runtime_process_error("runsc"))
    assert isinstance(error, DockerRuntimeNotRegisteredError)
    assert error.runtime_name == "runsc"
    # The message names the runtime and the help text offers the runc escape hatch.
    assert "runsc" in str(error)
    assert error.user_help_text is not None
    assert "runc" in error.user_help_text


def test_runtime_not_registered_error_ignores_unrelated_process_error(temp_mngr_ctx: MngrContext) -> None:
    provider = _make_docker_provider_with_runtime(temp_mngr_ctx, docker_runtime="runsc")
    unrelated = ProcessError(
        command=("docker", "run", "--runtime", "runsc", "debian:bookworm-slim"),
        stdout="",
        stderr="docker: Error response from daemon: pull access denied for debian\n",
        returncode=125,
    )
    assert provider._runtime_not_registered_error_or_none(unrelated) is None


def test_runtime_not_registered_error_none_when_runtime_unset(temp_mngr_ctx: MngrContext) -> None:
    # With the default runtime there is no `--runtime` flag, so even an output
    # carrying the marker is not attributable to a configured runtime.
    provider = _make_docker_provider_with_runtime(temp_mngr_ctx, docker_runtime=None)
    assert provider._runtime_not_registered_error_or_none(_make_unknown_runtime_process_error("runsc")) is None


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
def test_discover_hosts_raises_when_daemon_offline(temp_mngr_ctx: MngrContext) -> None:
    """discover_hosts raises ProviderUnavailableError (not []) when Docker is unreachable.

    Returning [] would let garbage collection treat an unreachable daemon as
    "this provider has zero hosts" and delete every host's volume data. Raising
    lets multi-provider callers (GC, listing) skip just this provider, the same
    way the Modal and Imbue Cloud providers behave.
    """
    provider = make_offline_docker_provider(temp_mngr_ctx)
    with pytest.raises(ProviderUnavailableError, match="not available"):
        provider.discover_hosts(cg=temp_mngr_ctx.concurrency_group)


@pytest.mark.docker_sdk
def test_discover_hosts_and_agents_raises_when_daemon_offline(temp_mngr_ctx: MngrContext) -> None:
    """discover_hosts_and_agents raises ProviderUnavailableError when Docker is unreachable."""
    provider = make_offline_docker_provider(temp_mngr_ctx)
    with pytest.raises(ProviderUnavailableError, match="not available"):
        provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)


class _ContainersRaising:
    """Stand-in for ``client.containers`` whose ``list`` always raises."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def list(self, **kwargs: object) -> list[docker.models.containers.Container]:
        raise self._error


class _FakeDockerClient:
    """Already-constructed Docker client whose container listing fails.

    Lets tests exercise the post-construction failure paths (daemon dies after
    the client was built, or the daemon responds with an API error) without a
    real daemon. The offline-provider helper only covers construction-time
    failure, which surfaces differently (a DockerException, not a raw transport
    error).
    """

    def __init__(self, error: Exception) -> None:
        self.containers = _ContainersRaising(error)


@pytest.mark.docker_sdk
def test_discover_hosts_raises_provider_unavailable_when_daemon_dies_after_connect(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A connection drop on an already-built client is treated as unavailable.

    When the daemon goes away after the client was constructed (e.g. a daemon
    restart mid-operation), ``containers.list()`` raises a raw
    ``requests.exceptions.ConnectionError`` -- not a ``DockerException``. This
    must still surface as ProviderUnavailableError so GC skips the provider
    rather than crashing or deleting its volumes.
    """
    provider = make_docker_provider(temp_mngr_ctx)
    # A version-pinned client skips the daemon ping at construction, so it builds
    # successfully even though the socket is dead -- mirroring a client that was
    # built while the daemon was up and is used after it went away.
    provider.__dict__["_docker_client"] = docker.DockerClient(
        base_url="unix:///nonexistent/docker.sock", version="1.40"
    )
    with pytest.raises(ProviderUnavailableError, match="not available"):
        provider.discover_hosts(cg=temp_mngr_ctx.concurrency_group)


def test_discover_hosts_propagates_api_error_without_marking_unavailable(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A responsive daemon that returns an API error is not mistaken for unavailable.

    ``docker.errors.APIError`` means the daemon was reached and answered with an
    error. Mislabeling it ProviderUnavailableError would make GC silently skip a
    healthy provider; the error must propagate so the failure is surfaced.
    """
    provider = make_docker_provider(temp_mngr_ctx)
    provider.__dict__["_docker_client"] = _FakeDockerClient(docker.errors.APIError("boom"))
    with pytest.raises(docker.errors.APIError):
        provider.discover_hosts(cg=temp_mngr_ctx.concurrency_group)


# =========================================================================
# Connection-Error Fallback State (running container, dead inner sshd)
# =========================================================================


class _FakeStatusContainer:
    """Minimal stand-in for a docker container with a settable status."""

    def __init__(self, status: str) -> None:
        self.status = status

    def reload(self) -> None:
        """No-op: the fake container's status does not change on reload."""


class _ContainersReturning:
    """Stand-in for ``client.containers`` whose ``list`` returns fixed containers."""

    def __init__(self, containers: list[_FakeStatusContainer]) -> None:
        self._containers = containers

    def list(self, **kwargs: object) -> list[_FakeStatusContainer]:
        return list(self._containers)


class _FakeDockerClientReturningContainers:
    """Already-constructed docker client whose container listing returns fixed containers.

    Lets tests exercise the daemon-backed container-status check without a real
    daemon (the daemon is ground truth for container lifecycle and is reachable
    without inner SSH).
    """

    def __init__(self, containers: list[_FakeStatusContainer]) -> None:
        self.containers = _ContainersReturning(containers)


def _docker_provider_with_containers(
    temp_mngr_ctx: MngrContext, containers: list[_FakeStatusContainer]
) -> DockerProviderInstance:
    provider = make_docker_provider(temp_mngr_ctx)
    provider.__dict__["_docker_client"] = _FakeDockerClientReturningContainers(containers)
    return provider


def test_connection_error_fallback_state_running_container_is_unauthenticated(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A running container whose inner sshd died reports UNAUTHENTICATED, not CRASHED.

    When agent enumeration fails with a connection error but the docker daemon
    still reports the container as running, the host is up -- we just can't get
    inside it -- so the fallback must report a non-offline state (mirroring
    mngr_imbue_cloud). Reporting CRASHED here makes minds' recovery flow skip
    the stop step of a host restart and then fail to start the live container.
    """
    provider = _docker_provider_with_containers(temp_mngr_ctx, [_FakeStatusContainer("running")])
    assert provider.get_connection_error_fallback_state(HostId(HOST_ID_A)) == HostState.UNAUTHENTICATED


def test_connection_error_fallback_state_stopped_container_returns_none(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A genuinely stopped container yields None so the default offline-state derivation stands."""
    provider = _docker_provider_with_containers(temp_mngr_ctx, [_FakeStatusContainer("exited")])
    assert provider.get_connection_error_fallback_state(HostId(HOST_ID_A)) is None


def test_connection_error_fallback_state_no_container_returns_none(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With no container for the host, None is returned so the default derivation stands."""
    provider = _docker_provider_with_containers(temp_mngr_ctx, [])
    assert provider.get_connection_error_fallback_state(HostId(HOST_ID_A)) is None


@pytest.mark.allow_warnings(match=r"Could not read docker container state for host .* during fallback")
def test_connection_error_fallback_state_daemon_unreachable_returns_none(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A daemon that drops during the out-of-band check yields None, not an exception.

    This hook runs inside the offline fallback, which must degrade gracefully:
    if the daemon goes away in the window after host enumeration, the fallback
    must keep the default offline-state derivation rather than propagate and
    make the whole host vanish from the listing.
    """
    provider = make_docker_provider(temp_mngr_ctx)
    provider.__dict__["_docker_client"] = _FakeDockerClient(docker.errors.APIError("boom"))
    assert provider.get_connection_error_fallback_state(HostId(HOST_ID_A)) is None


@pytest.mark.allow_warnings(match=r"Could not read docker container state for host .* during fallback")
def test_connection_error_fallback_state_daemon_transport_drop_returns_none(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A transport-level daemon drop during the out-of-band check yields None, not an exception.

    A socket-level drop surfaces as a ``requests.exceptions.ConnectionError`` rather
    than a ``docker.errors.DockerException`` (the docker SDK propagates the underlying
    transport error). The hook must treat it the same as any other daemon-unreachable
    condition and degrade to the default offline-state derivation, rather than letting
    it escape and break the offline fallback for the host.
    """
    provider = make_docker_provider(temp_mngr_ctx)
    provider.__dict__["_docker_client"] = _FakeDockerClient(requests.exceptions.ConnectionError("socket gone"))
    assert provider.get_connection_error_fallback_state(HostId(HOST_ID_A)) is None


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


# =========================================================================
# Build-image removal during destroy / GC
# =========================================================================


class _BuildRemovalImages:
    """Minimal stand-in for ``docker_client.images`` for build-image removal."""

    def __init__(self, *, present: bool, remove_error: Exception | None = None) -> None:
        self._present = present
        self._remove_error = remove_error
        self.remove_calls: list[dict] = []

    def list(self, name: str | None = None) -> list[object]:
        return [object()] if self._present else []

    def remove(self, tag: str, force: bool = False) -> None:
        self.remove_calls.append({"tag": tag, "force": force})
        if self._remove_error is not None:
            raise self._remove_error


class _BuildRemovalDockerClient:
    def __init__(self, images: _BuildRemovalImages) -> None:
        self.images = images


def test_remove_build_image_force_untags_present_image(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """_remove_build_image drops mngr's tag with force=True.

    Callers remove the host's container first, so force deletes the now-
    unreferenced image; force also lets mngr relinquish its tag when something
    unexpected still references the image, rather than failing with a 409.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    fake_images = _BuildRemovalImages(present=True)
    provider.__dict__["_docker_client"] = _BuildRemovalDockerClient(fake_images)

    provider._remove_build_image(HostId(HOST_ID_A))

    expected_tag = DockerProviderInstance._build_image_tag(HostId(HOST_ID_A))
    assert fake_images.remove_calls == [{"tag": expected_tag, "force": True}]


def test_remove_build_image_noops_when_absent(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """_remove_build_image does nothing when the build image is not present."""
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    fake_images = _BuildRemovalImages(present=False)
    provider.__dict__["_docker_client"] = _BuildRemovalDockerClient(fake_images)

    provider._remove_build_image(HostId(HOST_ID_A))

    assert fake_images.remove_calls == []


def _image_in_use_conflict() -> docker.errors.APIError:
    """The 409 Docker raises when a build image's last tag is removed while a container holds it."""
    response = requests.Response()
    response.status_code = 409
    return docker.errors.APIError(
        "409 Client Error: Conflict (must be forced) - container is using its referenced image",
        response=response,
    )


class _LeakedContainer:
    """A container in `_LeakedContainerDaemon`, referencing one host's build image."""

    def __init__(self, daemon: "_LeakedContainerDaemon", host_id: HostId, remove_error: Exception | None) -> None:
        self._daemon = daemon
        self.id = f"container-{host_id}"
        self.host_id = host_id
        self.status = "exited"
        self._remove_error = remove_error

    def reload(self) -> None:
        if self not in self._daemon.containers_present:
            raise docker.errors.NotFound(f"container {self.id} gone")

    def stop(self, timeout: int | None = None) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        if self._remove_error is not None:
            raise self._remove_error
        if self in self._daemon.containers_present:
            self._daemon.containers_present.remove(self)


class _LeakedContainerContainers:
    def __init__(self, daemon: "_LeakedContainerDaemon") -> None:
        self._daemon = daemon

    def list(self, all: bool = False, filters: dict | None = None) -> list[_LeakedContainer]:
        wanted_host_id: str | None = None
        for label in (filters or {}).get("label", []):
            if label.startswith(f"{LABEL_HOST_ID}="):
                wanted_host_id = label.split("=", 1)[1]
        return [c for c in self._daemon.containers_present if wanted_host_id in (None, str(c.host_id))]


class _LeakedContainerImages:
    def __init__(self, daemon: "_LeakedContainerDaemon") -> None:
        self._daemon = daemon

    def list(self, name: str | None = None) -> list[object]:
        return [object()] if name in self._daemon.image_tags else []

    def remove(self, tag: str, force: bool = False) -> None:
        if tag not in self._daemon.image_tags:
            raise docker.errors.NotFound(f"image {tag} not found")
        referenced = any(
            DockerProviderInstance._build_image_tag(c.host_id) == tag for c in self._daemon.containers_present
        )
        if referenced and not force:
            raise _image_in_use_conflict()
        self._daemon.image_tags.discard(tag)


class _LeakedContainerDaemon:
    """Stateful fake Docker client modeling the leaked-container/build-image conflict.

    The build image ``mngr-build-<host_id>`` is "in use" -- its unforced removal
    raises Docker's 409 conflict -- while a container for that host_id exists.
    force=True drops the tag regardless, and removing the container clears the
    conflict, mirroring real Docker semantics.
    """

    def __init__(self) -> None:
        self.image_tags: set[str] = set()
        self.containers_present: list[_LeakedContainer] = []
        self.images = _LeakedContainerImages(self)
        self.containers = _LeakedContainerContainers(self)

    def add_host_with_leaked_container(self, host_id: HostId, remove_error: Exception | None = None) -> None:
        self.image_tags.add(DockerProviderInstance._build_image_tag(host_id))
        self.containers_present.append(_LeakedContainer(self, host_id, remove_error))


def _write_destroyed_host_record(provider: DockerProviderInstance, host_id: str) -> OfflineHost:
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=host_id,
            host_name="h",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    provider._host_store.write_host_record(record)
    return provider._create_host_from_host_record(record)


def test_delete_host_destroys_leaked_container_then_removes_build_image(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """delete_host clears a leaked container so no trace of the host remains.

    A container left over from an earlier failed destroy still references the
    build image. delete_host re-runs destroy_host, which removes that container
    before its build image, leaving no container, no image, and no host record
    -- rather than force-untagging the image and leaking the container.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    daemon = _LeakedContainerDaemon()
    daemon.add_host_with_leaked_container(HostId(HOST_ID_A))
    provider.__dict__["_docker_client"] = daemon
    host = _write_destroyed_host_record(provider, HOST_ID_A)

    provider.delete_host(host)

    assert daemon.containers_present == []
    assert daemon.image_tags == set()
    assert provider._host_store.read_host_record(HostId(HOST_ID_A), use_cache=False) is None


@pytest.mark.allow_warnings(match=r"Failed to remove container for host")
def test_delete_host_records_leak_when_container_removal_stays_stuck(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A container that cannot be removed is surfaced as a leak, not a crash.

    When destroy_host still cannot remove the conflicting container, that
    container is recorded as a CleanupFailedGroup leak (which GC aggregates and
    continues past). The build image tag is force-dropped regardless -- mngr
    relinquishes its claim -- but the host record is kept (not forgotten) so the
    next sweep can retry the leftover.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    daemon = _LeakedContainerDaemon()
    daemon.add_host_with_leaked_container(HostId(HOST_ID_A), remove_error=docker.errors.APIError("stuck"))
    provider.__dict__["_docker_client"] = daemon
    host = _write_destroyed_host_record(provider, HOST_ID_A)

    with pytest.raises(CleanupFailedGroup) as exc_info:
        provider.delete_host(host)

    assert any(f.category == CleanupFailureCategory.HOST_RESOURCE_REMAINS for f in exc_info.value.failures)
    # The stuck container is reported as a leak and remains, but mngr force-drops
    # its build image tag regardless...
    assert daemon.containers_present != []
    assert daemon.image_tags == set()
    # ...and the host record is KEPT (not forgotten) so the next sweep can retry the leak.
    assert provider._host_store.read_host_record(HostId(HOST_ID_A), use_cache=False) is not None


class _RemoveDirectoryFailingVolume(LocalVolume):
    """LocalVolume whose remove_directory always fails (mirrors DockerVolume's `rm -rf` failure)."""

    def remove_directory(self, path: str) -> None:
        raise MngrError(f"failed to remove directory {path}")


@pytest.mark.allow_warnings(match=r"Failed to remove host volume for host")
def test_delete_host_records_volume_removal_failure_as_leak(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """A host volume that cannot be removed is recorded as a leak, not dismissed.

    remove_directory is idempotent on a missing path, so an exception means the
    volume data still exists and could not be removed -- a leftover resource,
    not a benign "no volume" case. delete_host must record it as a
    CleanupFailedGroup and keep the host record for the next sweep to retry.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    provider.__dict__["_state_volume"] = _RemoveDirectoryFailingVolume(root_path=tmp_path)
    provider.__dict__["_docker_client"] = _LeakedContainerDaemon()
    host = _write_destroyed_host_record(provider, HOST_ID_A)

    with pytest.raises(CleanupFailedGroup) as exc_info:
        provider.delete_host(host)

    assert any("host volume" in f.message for f in exc_info.value.failures)
    # The host record is kept on failure so the next sweep can retry.
    assert provider._host_store.read_host_record(HostId(HOST_ID_A), use_cache=False) is not None


def test_gvisor_runsc_without_overlay_none_is_ephemeral() -> None:
    """A runsc runtime registered without --overlay2=none has an ephemeral root fs."""
    runtimes = {"runsc": {"path": "/usr/bin/runsc"}}
    assert _is_gvisor_runtime_rootfs_ephemeral("runsc", runtimes) is True


def test_gvisor_runsc_with_other_args_but_not_overlay_none_is_ephemeral() -> None:
    """Other runtimeArgs do not make the root fs persistent -- only --overlay2=none does."""
    runtimes = {"runsc": {"path": "/usr/bin/runsc", "runtimeArgs": ["--network=none"]}}
    assert _is_gvisor_runtime_rootfs_ephemeral("runsc", runtimes) is True


def test_gvisor_runsc_with_overlay_none_is_persistent() -> None:
    """--overlay2=none writes the root layer through to the persistent Docker layer."""
    runtimes = {"runsc": {"path": "/usr/bin/runsc", "runtimeArgs": ["--overlay2=none"]}}
    assert _is_gvisor_runtime_rootfs_ephemeral("runsc", runtimes) is False


def test_non_gvisor_runtime_is_not_treated_as_ephemeral() -> None:
    """A non-runsc runtime's overlay semantics are unknown, so it is never flagged."""
    runtimes = {"runc": {"path": "runc"}}
    assert _is_gvisor_runtime_rootfs_ephemeral("runc", runtimes) is False


def test_unregistered_runtime_is_not_treated_as_ephemeral() -> None:
    """A runtime absent from the daemon config is left to Docker's own unknown-runtime error."""
    assert _is_gvisor_runtime_rootfs_ephemeral("runsc", {}) is False


# =========================================================================
# Recorded SSH Port Reconciliation (stale after Docker daemon restart)
# =========================================================================

# Ports from the real-world reproduction: a host reboot left the record at
# 52918 while docker reported 49315.
_RECORDED_STALE_SSH_PORT = 52918
_LIVE_SSH_PORT = 49315

_RECONCILE_HOST_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIRecordedHostKeyForPortReconcileTests"


class _FakePortedContainer:
    """Duck-typed stand-in for a running docker SDK container as read by
    ``_create_host_from_container``: labels for identity plus the live
    published-port mapping consumed by ``_read_container_ssh_port``."""

    def __init__(self, host_id: str, ssh_host_port: int) -> None:
        self.labels = build_container_labels(HostId(host_id), HostName("port-heal-host"), "test-docker", None)
        self.status = "running"
        self.id = f"fake-container-{host_id}"
        self.short_id = self.id[:10]
        self.ports = {f"{CONTAINER_SSH_PORT}/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(ssh_host_port)}]}

    def reload(self) -> None:
        """No-op: the fake's attributes are fixed."""


def _as_sdk_container(container: _FakePortedContainer) -> docker.models.containers.Container:
    # The provider only reads duck-typed attributes; cast satisfies the static type.
    return cast(docker.models.containers.Container, container)


def _write_host_record_with_ssh_port(
    provider: DockerProviderInstance,
    host_id: str,
    ssh_port: int,
) -> HostRecord:
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=host_id,
            host_name="port-heal-host",
            created_at=now,
            updated_at=now,
        ),
        ssh_host="127.0.0.1",
        ssh_port=ssh_port,
        ssh_host_public_key=_RECONCILE_HOST_PUBLIC_KEY,
    )
    provider._host_store.write_host_record(record)
    return record


class _WriteCountingHostStore(DockerHostStore):
    """DockerHostStore that counts record writes, so tests can assert the
    happy path (recorded port already matches) performs no redundant rewrite."""

    write_call_count: int = 0

    def write_host_record(self, host_record: HostRecord) -> None:
        self.write_call_count += 1
        super().write_host_record(host_record)


def _connector_ssh_port(host: Host) -> int | None:
    return host.connector.host.data.get("ssh_port")


def test_create_host_from_container_heals_stale_recorded_ssh_port(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """A record holding a dead port is healed from the container's live mapping.

    Docker does not preserve randomly-published host ports across a daemon
    restart, so the port persisted at create time can go stale. The connection
    must target the live port and the record must be rewritten with it, so
    workspaces reconnect after a reboot instead of dialing the dead port forever.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    _write_host_record_with_ssh_port(provider, HOST_ID_A, _RECORDED_STALE_SSH_PORT)
    container = _as_sdk_container(_FakePortedContainer(HOST_ID_A, _LIVE_SSH_PORT))

    with capture_loguru(level="INFO") as log_output:
        host = provider._create_host_from_container(container)

    assert host is not None
    assert _connector_ssh_port(host) == _LIVE_SSH_PORT

    stored_record = provider._host_store.read_host_record(HostId(HOST_ID_A), use_cache=False)
    assert stored_record is not None
    assert stored_record.ssh_port == _LIVE_SSH_PORT

    assert f"changed from {_RECORDED_STALE_SSH_PORT} to {_LIVE_SSH_PORT}" in log_output.getvalue()


def test_create_host_from_container_matching_port_is_not_rewritten(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When the recorded port matches the live mapping, nothing is rewritten or logged.

    The reconciliation must stay free on the happy path: no record write and no
    port-change log line -- and a cached Host with the matching port is reused
    as-is (preserving its SSH connection).
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    counting_store = _WriteCountingHostStore(volume=LocalVolume(root_path=tmp_path))
    provider.__dict__["_host_store"] = counting_store
    _write_host_record_with_ssh_port(provider, HOST_ID_A, _LIVE_SSH_PORT)
    container = _as_sdk_container(_FakePortedContainer(HOST_ID_A, _LIVE_SSH_PORT))

    cached_host = provider._create_host_from_container(container)
    assert cached_host is not None
    provider._host_by_id_cache[HostId(HOST_ID_A)] = cached_host
    write_count_before = counting_store.write_call_count

    with capture_loguru(level="INFO") as log_output:
        host = provider._create_host_from_container(container)

    assert host is cached_host
    assert counting_store.write_call_count == write_count_before
    assert "changed from" not in log_output.getvalue()


def test_port_reconciliation_replaces_stale_known_hosts_entry_for_new_port(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """A stale known_hosts key on the host's new port is replaced, not fatal.

    After a reboot, a container's new port may previously have belonged to a
    different container, leaving a known_hosts entry for [host]:port that does
    not match this host's key. Connection setup must replace that entry with
    the host's recorded public key so host-key verification succeeds.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    _write_host_record_with_ssh_port(provider, HOST_ID_A, _RECORDED_STALE_SSH_PORT)
    stale_entry = f"[127.0.0.1]:{_LIVE_SSH_PORT} ssh-ed25519 AAAAStaleKeyLeftByAnotherContainer"
    provider._known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    provider._known_hosts_path.write_text(f"{stale_entry}\n")
    container = _as_sdk_container(_FakePortedContainer(HOST_ID_A, _LIVE_SSH_PORT))

    host = provider._create_host_from_container(container)

    assert host is not None
    known_hosts_content = provider._known_hosts_path.read_text()
    assert stale_entry not in known_hosts_content
    assert f"[127.0.0.1]:{_LIVE_SSH_PORT} {_RECONCILE_HOST_PUBLIC_KEY}" in known_hosts_content


def test_port_reconciliation_invalidates_cached_host_with_stale_port(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """A cached Host still holding the old port is replaced by one on the live port.

    Without this, the provider keeps handing back the cached connector that
    dials the dead port even after the record is healed.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    _write_host_record_with_ssh_port(provider, HOST_ID_A, _RECORDED_STALE_SSH_PORT)
    stale_container = _as_sdk_container(_FakePortedContainer(HOST_ID_A, _RECORDED_STALE_SSH_PORT))
    stale_host = provider._create_host_from_container(stale_container)
    assert stale_host is not None
    provider._host_by_id_cache[HostId(HOST_ID_A)] = stale_host

    live_container = _as_sdk_container(_FakePortedContainer(HOST_ID_A, _LIVE_SSH_PORT))
    healed_host = provider._create_host_from_container(live_container)

    assert healed_host is not None
    assert healed_host is not stale_host
    assert _connector_ssh_port(healed_host) == _LIVE_SSH_PORT


def test_port_reconciliation_keeps_recorded_port_when_live_mapping_unreadable(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """An unreadable live port mapping keeps the recorded port and record intact.

    Reconciliation is best-effort healing: a container whose port mapping
    cannot be read (e.g. it stopped in the window since the caller's status
    check) must not break paths that previously worked, so the connection
    falls back to the recorded port, the record is not rewritten, and a
    warning is logged.
    """
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    _write_host_record_with_ssh_port(provider, HOST_ID_A, _RECORDED_STALE_SSH_PORT)
    container_without_mapping = _FakePortedContainer(HOST_ID_A, _LIVE_SSH_PORT)
    container_without_mapping.ports = {}

    with capture_loguru() as log_output:
        host = provider._create_host_from_container(_as_sdk_container(container_without_mapping))

    assert host is not None
    assert _connector_ssh_port(host) == _RECORDED_STALE_SSH_PORT

    stored_record = provider._host_store.read_host_record(HostId(HOST_ID_A), use_cache=False)
    assert stored_record is not None
    assert stored_record.ssh_port == _RECORDED_STALE_SSH_PORT

    assert "Could not read live SSH port" in log_output.getvalue()
