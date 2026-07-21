"""Integration tests for Docker provider that exercise real Docker operations.

These tests require a running Docker daemon but do NOT require networking
(port publishing). They test container management, exec, labels, discovery,
snapshots, and host store integration using the Docker API directly.

Marked with @pytest.mark.docker_sdk and @pytest.mark.acceptance so they only
run in CI acceptance test shards (not in the default local test suite).
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import docker
import docker.errors
import docker.models.containers
import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostResizeRequest
from imbue.mngr.interfaces.data_types import HostResizeValue
from imbue.mngr.interfaces.data_types import HostResourceLimits
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.docker.host_store import ContainerConfig
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.docker.instance import CONTAINER_ENTRYPOINT
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.instance import LABEL_HOST_ID
from imbue.mngr.providers.docker.instance import LABEL_HOST_NAME
from imbue.mngr.providers.docker.instance import LABEL_PROVIDER
from imbue.mngr.providers.docker.instance import LABEL_TAGS
from imbue.mngr.providers.docker.instance import build_container_labels
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.acceptance]

# Use a longer timeout since Docker operations can be slow (image pulls, etc.)
DOCKER_TEST_TIMEOUT = 120

# Use busybox for test containers -- much smaller than debian:bookworm-slim (~5MB vs ~80MB),
# which matters a lot when using the VFS storage driver (full copy per container).
# All commands used in tests (echo, cat, tr, false, sleep, tail) are busybox builtins.
TEST_IMAGE = "busybox:latest"


def _create_test_container(
    provider: DockerProviderInstance,
    host_id: HostId | None = None,
    name: str = "test-host",
    tags: dict[str, str] | None = None,
) -> tuple[docker.models.containers.Container, HostId]:
    """Create a bare container with labels (no SSH setup).

    The container name uses the provider's MNGR prefix so that
    ``_list_containers`` (which filters by prefix) can find it during
    cleanup.
    """
    if host_id is None:
        host_id = HostId.generate()
    labels = build_container_labels(host_id, HostName(name), str(provider.name), tags)
    prefix = provider.mngr_ctx.config.prefix
    container_name = f"{prefix}integ-{get_short_random_string()}"
    container = provider._docker_client.containers.run(
        image=TEST_IMAGE,
        name=container_name,
        command=CONTAINER_ENTRYPOINT,
        detach=True,
        labels=labels,
    )
    return container, host_id


# =========================================================================
# Container Creation and Labels
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_container_created_with_correct_labels(docker_provider: DockerProviderInstance) -> None:
    """Verify that containers are created with the expected mngr labels."""
    host_id = HostId.generate()
    container, _ = _create_test_container(
        docker_provider, host_id=host_id, name="label-test", tags={"env": "test", "team": "infra"}
    )

    container.reload()
    labels = container.labels

    assert labels[LABEL_HOST_ID] == str(host_id)
    assert labels[LABEL_HOST_NAME] == "label-test"
    assert labels[LABEL_PROVIDER] == str(docker_provider.name)
    assert '"env": "test"' in labels[LABEL_TAGS]
    assert '"team": "infra"' in labels[LABEL_TAGS]


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_container_is_running_after_creation(docker_provider: DockerProviderInstance) -> None:
    container, _ = _create_test_container(docker_provider)
    assert docker_provider._is_container_running(container) is True


# =========================================================================
# Container Discovery
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_find_container_by_host_id(docker_provider: DockerProviderInstance) -> None:
    _, host_id = _create_test_container(docker_provider)
    found = docker_provider._find_container_by_host_id(host_id)
    assert found is not None
    assert found.labels[LABEL_HOST_ID] == str(host_id)


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_find_container_by_host_id_returns_none_for_unknown(docker_provider: DockerProviderInstance) -> None:
    found = docker_provider._find_container_by_host_id(HostId.generate())
    assert found is None


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_find_container_by_name(docker_provider: DockerProviderInstance) -> None:
    _create_test_container(docker_provider, name="discoverable")
    found = docker_provider._find_container_by_name(HostName("discoverable"))
    assert found is not None


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_list_containers_returns_managed_containers(docker_provider: DockerProviderInstance) -> None:
    _create_test_container(docker_provider, name="list-a")
    _create_test_container(docker_provider, name="list-b")
    containers = docker_provider._list_containers()
    assert len(containers) >= 2


# =========================================================================
# Docker Exec
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_exec_in_container_returns_output(docker_provider: DockerProviderInstance) -> None:
    container, _ = _create_test_container(docker_provider)
    exit_code, output = docker_provider._exec_in_container(container, "echo hello-from-exec")
    assert exit_code == 0
    assert "hello-from-exec" in output


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_exec_in_container_returns_nonzero_on_failure(docker_provider: DockerProviderInstance) -> None:
    container, _ = _create_test_container(docker_provider)
    exit_code, _ = docker_provider._exec_in_container(container, "false")
    assert exit_code != 0


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_exec_detach_returns_immediately(docker_provider: DockerProviderInstance) -> None:
    container, _ = _create_test_container(docker_provider)
    exit_code, output = docker_provider._exec_in_container(container, "sleep 3600", detach=True)
    assert exit_code == 0
    assert output == ""


# =========================================================================
# Image Pull
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_pull_image_succeeds(docker_provider: DockerProviderInstance) -> None:
    result = docker_provider._pull_image("debian:bookworm-slim")
    assert result == "debian:bookworm-slim"


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
@pytest.mark.flaky
def test_pull_image_not_found_raises(docker_provider: DockerProviderInstance) -> None:
    # Pulling a nonexistent image should surface a clean "image not found"
    # MngrError (the registry returns a 404 -> docker.errors.ImageNotFound).
    # When Docker Hub is unreachable (slow / timed-out registry connection on a
    # CI runner), the same pull instead fails with a generic connectivity
    # APIError before the registry can return its 404. That is an environmental
    # flake unrelated to the not-found path under test, so skip rather than fail.
    with pytest.raises(MngrError) as exc_info:
        docker_provider._pull_image("nonexistent-image-that-does-not-exist:99999")
    message = str(exc_info.value)
    if "Docker image not found" not in message:
        pytest.skip(f"Docker registry unreachable; cannot exercise the not-found path: {message}")


@pytest.mark.docker
@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
def test_build_image_from_dockerfile(docker_provider: DockerProviderInstance, tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {TEST_IMAGE}\nRUN echo 'built' > /build-marker.txt\n")

    tag = "mngr-test-build-image"
    result = docker_provider._build_image([f"--file={dockerfile}", str(tmp_path)], tag)
    assert result == tag
    # Verify the image was loaded into the local Docker daemon. Both
    # `docker build` and `depot build --load` import the result into the
    # local daemon, so this works for either configured builder and gives
    # the test something to assert beyond the tag echo.
    inspect = docker_provider._run_docker_creation_command(
        ["image", "inspect", "--format", "{{.Id}}", tag], timeout=10
    )
    assert inspect.stdout.strip(), f"image {tag} not found in local daemon after build"


# =========================================================================
# Container Lifecycle (Stop / Start / Remove)
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_container_stop_and_start(docker_provider: DockerProviderInstance) -> None:
    container, host_id = _create_test_container(docker_provider)
    assert docker_provider._is_container_running(container) is True

    container.stop(timeout=5)
    container.reload()
    assert container.status in ("exited", "stopped")

    container.start()
    assert docker_provider._is_container_running(container) is True


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_container_remove(docker_provider: DockerProviderInstance) -> None:
    container, host_id = _create_test_container(docker_provider)
    container.remove(force=True)

    found = docker_provider._find_container_by_host_id(host_id)
    assert found is None


# =========================================================================
# Snapshots (docker commit)
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_docker_commit_creates_image(docker_provider: DockerProviderInstance) -> None:
    """Verify docker commit works on a running container."""
    container, host_id = _create_test_container(docker_provider)

    # Write something unique to the container filesystem
    docker_provider._exec_in_container(container, "echo 'snapshot-data' > /snapshot-test.txt")

    # Commit the container
    committed_image = container.commit(repository="mngr-test-snapshot", tag="test")
    assert committed_image.id is not None

    # Clean up
    try:
        docker_provider._docker_client.images.remove(committed_image.id, force=True)
    except docker.errors.DockerException:
        pass


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_snapshot_roundtrip_preserves_filesystem(docker_provider: DockerProviderInstance) -> None:
    """Verify that committing a container and running from the image preserves files."""
    container, host_id = _create_test_container(docker_provider, name="snap-source")

    # Write test data
    docker_provider._exec_in_container(container, "echo 'persisted-content-12345' > /persist.txt")

    # Commit
    committed_image = container.commit(repository="mngr-snap-roundtrip", tag="v1")

    try:
        # Run new container from committed image
        new_container = docker_provider._docker_client.containers.run(
            image=committed_image.id,
            command=CONTAINER_ENTRYPOINT,
            detach=True,
        )

        try:
            exit_code, output = docker_provider._exec_in_container(new_container, "cat /persist.txt")
            assert exit_code == 0
            assert "persisted-content-12345" in output
        finally:
            new_container.remove(force=True)
    finally:
        try:
            docker_provider._docker_client.images.remove(committed_image.id, force=True)
        except docker.errors.DockerException:
            pass


# =========================================================================
# Host Store Integration with Real Containers
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_host_store_write_and_discover(docker_provider: DockerProviderInstance) -> None:
    """Create a container, write a host record, and verify discovery works."""
    container, host_id = _create_test_container(docker_provider, name="store-test")

    now = datetime.now(timezone.utc)
    host_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="store-test",
        created_at=now,
        updated_at=now,
    )

    host_record = HostRecord(
        certified_host_data=host_data,
        ssh_host="127.0.0.1",
        last_discovered_ssh_port=12345,
        ssh_host_public_key="ssh-ed25519 AAAA-test-key",
        config=ContainerConfig(start_args=("--cpus=2", "--memory=4g")),
        container_id=container.id,
    )
    docker_provider._host_store.write_host_record(host_record)

    # Verify record can be read back
    read_back = docker_provider._host_store.read_host_record(host_id)
    assert read_back is not None
    assert read_back.container_id == container.id
    assert read_back.ssh_host == "127.0.0.1"
    assert read_back.last_discovered_ssh_port == 12345

    # Verify container is discoverable by host_id
    found = docker_provider._find_container_by_host_id(host_id)
    assert found is not None


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_save_failed_host_record(docker_provider: DockerProviderInstance) -> None:
    """Verify that _save_failed_host_record creates a valid host record."""
    host_id = HostId.generate()
    docker_provider._save_failed_host_record(
        host_id=host_id,
        host_name=HostName("failed-host"),
        tags={"env": "test"},
        failure_reason="Container startup failed",
        build_log="error: something went wrong",
    )

    record = docker_provider._host_store.read_host_record(host_id)
    assert record is not None
    assert record.certified_host_data.failure_reason == "Container startup failed"
    assert record.certified_host_data.build_log == "error: something went wrong"
    assert record.certified_host_data.host_name == "failed-host"
    assert record.certified_host_data.user_tags == {"env": "test"}
    # Failed hosts have no SSH info
    assert record.ssh_host is None
    assert record.last_discovered_ssh_port is None


# =========================================================================
# Tag Reading from Real Containers
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_get_host_tags_from_running_container(docker_provider: DockerProviderInstance) -> None:
    """Verify get_host_tags reads tags from actual container labels."""
    container, host_id = _create_test_container(
        docker_provider, name="tag-read", tags={"env": "staging", "version": "1.0"}
    )

    # Write a host record so get_host_tags can fall back if needed
    now = datetime.now(timezone.utc)
    host_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="tag-read",
        user_tags={"env": "staging", "version": "1.0"},
        created_at=now,
        updated_at=now,
    )
    host_record = HostRecord(certified_host_data=host_data, container_id=container.id)
    docker_provider._host_store.write_host_record(host_record)

    tags = docker_provider.get_host_tags(host_id)
    assert tags == {"env": "staging", "version": "1.0"}


# =========================================================================
# Host Resources
# =========================================================================


# =========================================================================
# Entrypoint and Container Behavior
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_container_entrypoint_keeps_running(docker_provider: DockerProviderInstance) -> None:
    """Verify the CONTAINER_ENTRYPOINT keeps the container alive."""
    container, _ = _create_test_container(docker_provider)

    # Container should be running
    assert docker_provider._is_container_running(container) is True

    # PID 1 should be the shell running the entrypoint
    # Use /proc/1/cmdline since ps may not be installed in minimal images
    exit_code, output = docker_provider._exec_in_container(container, "cat /proc/1/cmdline | tr '\\0' ' '")
    assert exit_code == 0
    # The entrypoint runs: sh -c "trap 'exit 0' TERM; tail -f /dev/null & wait"
    assert "sh" in output


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_container_responds_to_sigterm(docker_provider: DockerProviderInstance) -> None:
    """Verify the container exits cleanly on SIGTERM (docker stop)."""
    container, _ = _create_test_container(docker_provider)
    assert docker_provider._is_container_running(container) is True

    container.stop(timeout=5)
    container.reload()
    assert container.status in ("exited", "stopped")
    # Exit code 0 means clean shutdown via trap
    assert container.attrs is not None
    assert container.attrs["State"]["ExitCode"] == 0


# =========================================================================
# Filesystem Persistence Across Stop/Start
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_filesystem_persists_across_stop_start(docker_provider: DockerProviderInstance) -> None:
    """Verify that files written to a container persist across stop/start."""
    container, _ = _create_test_container(docker_provider)

    docker_provider._exec_in_container(container, "echo 'survive-stop' > /test-persist.txt")

    container.stop(timeout=5)
    container.start()

    exit_code, output = docker_provider._exec_in_container(container, "cat /test-persist.txt")
    assert exit_code == 0
    assert "survive-stop" in output


# =========================================================================
# DockerVolume Tests
# =========================================================================


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_docker_volume_write_and_read(docker_provider: DockerProviderInstance) -> None:
    """Verify DockerVolume can write and read files via the state container."""
    volume = docker_provider._state_volume
    volume.write_files({"test/hello.txt": b"world"})
    result = volume.read_file("test/hello.txt")
    assert result == b"world"


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_docker_volume_listdir(docker_provider: DockerProviderInstance) -> None:
    """Verify DockerVolume.listdir returns entries."""
    volume = docker_provider._state_volume
    volume.write_files({"listdir-test/a.txt": b"a", "listdir-test/b.txt": b"b"})
    entries = volume.listdir("listdir-test")
    names = [e.path.rsplit("/", 1)[-1] for e in entries]
    assert "a.txt" in names
    assert "b.txt" in names


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_docker_volume_remove_file(docker_provider: DockerProviderInstance) -> None:
    """Verify DockerVolume.remove_file deletes a file."""
    volume = docker_provider._state_volume
    volume.write_files({"rm-test/file.txt": b"data"})
    volume.remove_file("rm-test/file.txt")
    with pytest.raises(FileNotFoundError):
        volume.read_file("rm-test/file.txt")


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_docker_volume_remove_directory(docker_provider: DockerProviderInstance) -> None:
    """Verify DockerVolume.remove_directory recursively removes a directory."""
    volume = docker_provider._state_volume
    volume.write_files({"rmdir-test/sub/file.txt": b"data"})
    volume.remove_directory("rmdir-test")
    with pytest.raises(FileNotFoundError):
        volume.listdir("rmdir-test")


# =========================================================================
# Resource Resizing
# =========================================================================


def _write_resize_host_record(
    provider: DockerProviderInstance,
    container: docker.models.containers.Container,
    host_id: HostId,
) -> None:
    now = datetime.now(timezone.utc)
    provider._host_store.write_host_record(
        HostRecord(
            certified_host_data=CertifiedHostData(
                host_id=str(host_id),
                host_name="resize-test",
                user_tags={},
                snapshots=[],
                created_at=now,
                updated_at=now,
            ),
            config=ContainerConfig(start_args=(), image=TEST_IMAGE),
            container_id=container.id,
        )
    )


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_resize_host_applies_limits_live_to_running_container(docker_provider: DockerProviderInstance) -> None:
    """A resize of a running container applies immediately via docker update.

    The report's configured and actual values agree (byte-exact through the
    GiB conversion), and the container's HostConfig carries the new limits with
    memory-swap pinned to the memory limit.
    """
    container, host_id = _create_test_container(docker_provider)
    _write_resize_host_record(docker_provider, container, host_id)

    report = docker_provider.resize_host(
        host_id,
        HostResizeRequest(cpu_count=HostResizeValue(value=1), memory_gib=HostResizeValue(value=1)),
    )

    assert report.configured == HostResourceLimits(cpu_count=1.0, memory_gib=1.0)
    assert report.actual == HostResourceLimits(cpu_count=1.0, memory_gib=1.0)
    container.reload()
    host_config = container.attrs["HostConfig"]
    assert host_config["NanoCpus"] == 1_000_000_000
    assert host_config["Memory"] == 1024**3
    assert host_config["MemorySwap"] == 1024**3

    # The read mode sees the same facts.
    read_report = docker_provider.get_host_resource_limits(host_id)
    assert read_report.configured == report.configured
    assert read_report.actual == report.actual


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_resize_host_on_stopped_container_persists_and_reports_no_actual(
    docker_provider: DockerProviderInstance,
) -> None:
    """Resizing a stopped container persists (docker keeps HostConfig across starts); actual is absent."""
    container, host_id = _create_test_container(docker_provider)
    _write_resize_host_record(docker_provider, container, host_id)
    container.stop(timeout=1)

    report = docker_provider.resize_host(
        host_id,
        HostResizeRequest(cpu_count=HostResizeValue(value=2), memory_gib=HostResizeValue(value=1)),
    )

    # Nothing to probe on a stopped container; the configured values are the
    # durable facts and docker's own HostConfig already carries them.
    assert report.configured == HostResourceLimits(cpu_count=2.0, memory_gib=1.0)
    assert report.actual is None
    container.reload()
    assert container.attrs["HostConfig"]["NanoCpus"] == 2_000_000_000

    record = docker_provider._host_store.read_host_record(host_id, use_cache=False)
    assert record is not None and record.resources is not None
    assert record.resources.cpu is not None and record.resources.cpu.count == 2
    assert record.resources.memory_gb == 1.0


@pytest.mark.timeout(DOCKER_TEST_TIMEOUT)
@pytest.mark.docker_sdk
def test_get_resize_capabilities_reports_daemon_allotment_ceilings(
    docker_provider: DockerProviderInstance,
) -> None:
    capabilities = docker_provider.get_resize_capabilities()

    assert capabilities.is_resize_supported
    assert capabilities.cpu is not None and capabilities.memory_gib is not None
    # Docker's default is unlimited for both dimensions.
    assert capabilities.cpu.default_value is None
    assert capabilities.memory_gib.default_value is None
    assert capabilities.cpu.ceiling is not None and capabilities.cpu.ceiling >= 1
    assert capabilities.memory_gib.ceiling is not None and capabilities.memory_gib.ceiling >= 1
