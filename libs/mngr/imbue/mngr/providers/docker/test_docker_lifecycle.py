from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotNotFoundError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_docker_provider
from imbue.mngr.providers.docker.testing import make_docker_provider_with_cleanup

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(120)]


@pytest.fixture
def docker_provider(temp_mngr_ctx: MngrContext) -> Generator[DockerProviderInstance, None, None]:
    yield from make_docker_provider_with_cleanup(temp_mngr_ctx)


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_host_creates_container_with_ssh(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-ssh"))
    assert isinstance(host, Host)
    result = host.execute_command("echo hello")
    assert result.success
    assert "hello" in result.stdout


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_host_with_tags(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-tags"), tags={"env": "test", "team": "infra"})
    assert isinstance(host, Host)

    tags = docker_provider.get_host_tags(host.id)
    assert tags == {"env": "test", "team": "infra"}


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_host_with_custom_image(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(
        HostName("test-image"),
        image=ImageReference("python:3.11-slim"),
    )
    assert isinstance(host, Host)
    result = host.execute_command("python3 --version")
    assert result.success
    assert "Python" in result.stdout


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_host_with_resource_limits(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(
        HostName("test-resources"),
        start_args=["--cpus=2", "--memory=2g"],
    )
    assert isinstance(host, Host)
    result = host.execute_command("echo ok")
    assert result.success


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_stop_host_stops_container(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-stop"))
    docker_provider.stop_host(host, create_snapshot=False)

    # Host should now be offline
    host_obj = docker_provider.get_host(host.id)
    assert isinstance(host_obj, OfflineHost)


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_stop_host_with_snapshot(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-snap-stop"))
    docker_provider.stop_host(host, create_snapshot=True)

    snapshots = docker_provider.list_snapshots(host.id)
    assert len(snapshots) >= 1
    assert any(str(s.name).startswith("stop-") for s in snapshots)


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_start_host_restarts_stopped_container(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-restart"))
    host.execute_command("touch /mngr/marker.txt")
    docker_provider.stop_host(host, create_snapshot=False)

    restarted = docker_provider.start_host(host.id)
    assert isinstance(restarted, Host)
    result = restarted.execute_command("cat /mngr/marker.txt")
    assert result.success


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_start_host_filesystem_preserved_across_stop_start(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-fs-preserve"))
    host.execute_command("echo 'test content' > /tmp/myfile.txt")
    docker_provider.stop_host(host, create_snapshot=False)

    restarted = docker_provider.start_host(host.id)
    result = restarted.execute_command("cat /tmp/myfile.txt")
    assert result.success
    assert "test content" in result.stdout


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_start_host_on_running_host_returns_same_host(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-already-running"))
    restarted = docker_provider.start_host(host.id)
    assert isinstance(restarted, Host)


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_destroy_host_removes_container(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-destroy"))
    host_id = host.id
    docker_provider.destroy_host(host, delete_snapshots=True)

    with pytest.raises(HostNotFoundError):
        docker_provider.get_host(host_id)


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_get_host_by_id(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-get-id"))
    found = docker_provider.get_host(host.id)
    assert found.id == host.id


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_get_host_by_name(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-get-name"))
    found = docker_provider.get_host(HostName("test-get-name"))
    assert found.id == host.id


@pytest.mark.docker_sdk
def test_get_host_not_found_raises_error(docker_provider: DockerProviderInstance) -> None:
    with pytest.raises(HostNotFoundError):
        docker_provider.get_host(HostId.generate())


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_hosts_includes_created_host(
    docker_provider: DockerProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    host = docker_provider.create_host(HostName("test-list"))
    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    host_ids = {h.host_id for h in hosts}
    assert host.id in host_ids


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_snapshot(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-snapshot"))
    snapshot_id = docker_provider.create_snapshot(host, SnapshotName("test-snap"))
    assert snapshot_id is not None

    snapshots = docker_provider.list_snapshots(host)
    assert len(snapshots) == 1
    assert snapshots[0].name == SnapshotName("test-snap")


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_delete_snapshot(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-del-snap"))
    snapshot_id = docker_provider.create_snapshot(host, SnapshotName("to-delete"))

    docker_provider.delete_snapshot(host, snapshot_id)

    snapshots = docker_provider.list_snapshots(host)
    assert len(snapshots) == 0


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_delete_nonexistent_snapshot_raises_error(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-del-nonexist"))
    with pytest.raises(SnapshotNotFoundError):
        docker_provider.delete_snapshot(host, SnapshotId("sha256:nonexistent0000000000000000000000"))


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_set_host_tags_raises_mngr_error(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-tags-immutable"))
    with pytest.raises(MngrError, match="does not support mutable tags"):
        docker_provider.set_host_tags(host, {"new": "tag"})


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_rename_host(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-rename"))
    docker_provider.rename_host(host, HostName("renamed-host"))

    # Verify lookup by ID works
    found_by_id = docker_provider.get_host(host.id)
    assert found_by_id.get_certified_data().host_name == "renamed-host"

    # Verify lookup by new name works (even though container label has old name)
    found_by_name = docker_provider.get_host(HostName("renamed-host"))
    assert found_by_name.id == host.id


@pytest.mark.docker_sdk
def test_close_closes_docker_client(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx, "test-close")
    # Access the client to initialize it
    _ = provider._docker_client
    provider.close()


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_on_connection_error_clears_caches(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-conn-err"))
    # Populate caches
    docker_provider.get_host(host.id)
    # Should not raise
    docker_provider.on_connection_error(host.id)


# =========================================================================
# SSH Setup Verification
# =========================================================================


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_ssh_service_running_after_create(docker_provider: DockerProviderInstance) -> None:
    """Verify that sshd is running inside the container after create_host."""
    host = docker_provider.create_host(HostName("test-sshd"))
    result = host.execute_command("pgrep -x sshd")
    assert result.success, f"sshd not running: {result.stderr}"


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_ssh_packages_installed_after_create(docker_provider: DockerProviderInstance) -> None:
    """Verify required packages are installed in the container after create_host."""
    host = docker_provider.create_host(HostName("test-pkgs"))
    result = host.execute_command("dpkg -l openssh-server")
    assert result.success, f"openssh-server not installed: {result.stderr}"


# =========================================================================
# Snapshot Restore
# =========================================================================


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_stop_with_snapshot_then_start_preserves_data(docker_provider: DockerProviderInstance) -> None:
    """Core snapshot workflow: write data, stop with snapshot, start, verify data."""
    host = docker_provider.create_host(HostName("test-snap-restore"))
    host.execute_command("echo 'snapshot-payload-xyz' > /tmp/snapshot-data.txt")

    docker_provider.stop_host(host, create_snapshot=True)

    restarted = docker_provider.start_host(host.id)
    assert isinstance(restarted, Host)
    result = restarted.execute_command("cat /tmp/snapshot-data.txt")
    assert result.success
    assert "snapshot-payload-xyz" in result.stdout


# =========================================================================
# Dockerfile-based Host Creation
# =========================================================================


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_host_with_dockerfile(docker_provider: DockerProviderInstance, tmp_path: Path) -> None:
    """Verify create_host works with a custom Dockerfile at the API level."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM debian:bookworm-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        "openssh-server tmux python3 rsync && rm -rf /var/lib/apt/lists/*\n"
        "RUN echo 'dockerfile-marker-content' > /dockerfile-marker.txt\n"
    )
    host = docker_provider.create_host(
        HostName("test-dockerfile"),
        build_args=[f"--file={dockerfile}", str(tmp_path)],
    )
    assert isinstance(host, Host)
    result = host.execute_command("cat /dockerfile-marker.txt")
    assert result.success
    assert "dockerfile-marker-content" in result.stdout


# =========================================================================
# Agent Data Persistence
# =========================================================================


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_persist_and_list_agent_data(docker_provider: DockerProviderInstance) -> None:
    """Verify agent data can be persisted and listed for a host."""
    host = docker_provider.create_host(HostName("test-agent-data"))
    agent_id = str(AgentId.generate())
    agent_data = {"id": agent_id, "name": "test-agent", "status": "running"}

    docker_provider.persist_agent_data(host.id, agent_data)
    records = docker_provider.list_persisted_agent_data_for_host(host.id)

    assert len(records) == 1
    assert records[0]["id"] == agent_id
    assert records[0]["name"] == "test-agent"


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_remove_persisted_agent_data(docker_provider: DockerProviderInstance) -> None:
    """Verify agent data can be removed after persisting."""
    host = docker_provider.create_host(HostName("test-rm-agent"))
    agent_id = AgentId.generate()
    agent_data = {"id": str(agent_id), "name": "ephemeral"}

    docker_provider.persist_agent_data(host.id, agent_data)
    docker_provider.remove_persisted_agent_data(host.id, agent_id)

    records = docker_provider.list_persisted_agent_data_for_host(host.id)
    assert len(records) == 0


# =========================================================================
# Stopped Host Behavior
# =========================================================================


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_get_host_returns_offline_host_when_stopped(docker_provider: DockerProviderInstance) -> None:
    """Verify that get_host returns an OfflineHost for a stopped container."""
    host = docker_provider.create_host(HostName("test-offline"))
    docker_provider.stop_host(host, create_snapshot=False)

    found = docker_provider.get_host(host.id)
    assert isinstance(found, OfflineHost)


@pytest.mark.docker_sdk
def test_start_failed_host_raises_error(docker_provider: DockerProviderInstance) -> None:
    """Verify that start_host on a failed host raises MngrError."""
    host_id = HostId.generate()
    docker_provider._save_failed_host_record(
        host_id=host_id,
        host_name=HostName("failed-host"),
        tags={},
        failure_reason="Intentional test failure",
        build_log="",
    )

    with pytest.raises(MngrError, match="failed during creation"):
        docker_provider.start_host(host_id)


# =========================================================================
# Release Tests (comprehensive / slower)
# =========================================================================


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_multiple_snapshots_ordering(docker_provider: DockerProviderInstance) -> None:
    """Verify multiple snapshots are tracked and listed in recency order."""
    host = docker_provider.create_host(HostName("test-multi-snap"))

    docker_provider.create_snapshot(host, SnapshotName("snap-a"))
    docker_provider.create_snapshot(host, SnapshotName("snap-b"))
    docker_provider.create_snapshot(host, SnapshotName("snap-c"))

    snapshots = docker_provider.list_snapshots(host)
    assert len(snapshots) == 3
    # Most recent first (recency_idx 0 = most recent)
    assert snapshots[0].name == SnapshotName("snap-c")
    assert snapshots[0].recency_idx == 0
    assert snapshots[2].name == SnapshotName("snap-a")
    assert snapshots[2].recency_idx == 2


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_destroy_with_snapshots_cleans_up_images(docker_provider: DockerProviderInstance) -> None:
    """Verify destroy_host with delete_snapshots removes snapshot images."""
    host = docker_provider.create_host(HostName("test-destroy-snap"))
    docker_provider.create_snapshot(host, SnapshotName("to-cleanup"))

    host_id = host.id
    docker_provider.destroy_host(host, delete_snapshots=True)

    # Host record should be gone
    with pytest.raises(HostNotFoundError):
        docker_provider.get_host(host_id)

    # Snapshot image should be removed (or at least not trackable)
    snapshots = docker_provider.list_snapshots(host_id)
    assert len(snapshots) == 0


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_hosts_excludes_destroyed_by_default(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Verify destroyed hosts are excluded from discover_hosts by default."""
    host = docker_provider.create_host(HostName("test-destroyed-list"))
    host_id = host.id
    docker_provider.destroy_host(host, delete_snapshots=True)

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    host_ids = {h.host_id for h in hosts}
    assert host_id not in host_ids


@pytest.mark.release
@pytest.mark.docker_sdk
def test_create_host_with_bad_image_fails(docker_provider: DockerProviderInstance) -> None:
    """Verify create_host with a nonexistent image raises MngrError and saves a failed record."""
    with pytest.raises(MngrError):
        docker_provider.create_host(
            HostName("test-bad-image"),
            image=ImageReference("nonexistent-image-does-not-exist:99999"),
        )


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_multiple_hosts_isolated(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Verify multiple hosts are independently addressable and isolated."""
    host_a = docker_provider.create_host(HostName("test-iso-a"))
    host_b = docker_provider.create_host(HostName("test-iso-b"))

    host_a.execute_command("echo 'from-a' > /tmp/identity.txt")
    host_b.execute_command("echo 'from-b' > /tmp/identity.txt")

    result_a = host_a.execute_command("cat /tmp/identity.txt")
    result_b = host_b.execute_command("cat /tmp/identity.txt")

    assert "from-a" in result_a.stdout
    assert "from-b" in result_b.stdout

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    host_ids = {h.host_id for h in hosts}
    assert host_a.id in host_ids
    assert host_b.id in host_ids


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_persist_multiple_agents_for_same_host(docker_provider: DockerProviderInstance) -> None:
    """Verify multiple agent data records can be persisted for one host."""
    host = docker_provider.create_host(HostName("test-multi-agent"))
    agent_id_1 = str(AgentId.generate())
    agent_id_2 = str(AgentId.generate())

    docker_provider.persist_agent_data(host.id, {"id": agent_id_1, "type": "claude"})
    docker_provider.persist_agent_data(host.id, {"id": agent_id_2, "type": "codex"})

    records = docker_provider.list_persisted_agent_data_for_host(host.id)
    assert len(records) == 2
    agent_ids = {r["id"] for r in records}
    assert agent_ids == {agent_id_1, agent_id_2}
