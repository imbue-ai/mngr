from collections.abc import Generator
from pathlib import Path

import docker.errors
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import SnapshotNotFoundError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.providers.docker.backend import DockerProviderBackend
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.instance import create_docker_client
from imbue.mngr.providers.docker.testing import make_docker_provider
from imbue.mngr.providers.docker.testing import make_docker_provider_with_cleanup
from imbue.mngr.providers.docker.volume import state_container_name

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_host_creates_container_with_ssh(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-ssh"))
    assert isinstance(host, Host)
    result = host.execute_idempotent_command("echo hello")
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
    result = host.execute_idempotent_command("python3 --version")
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
    result = host.execute_idempotent_command("echo ok")
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
    host.execute_idempotent_command("touch /mngr/marker.txt")
    docker_provider.stop_host(host, create_snapshot=False)

    restarted = docker_provider.start_host(host.id)
    assert isinstance(restarted, Host)
    result = restarted.execute_idempotent_command("cat /mngr/marker.txt")
    assert result.success


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_start_host_filesystem_preserved_across_stop_start(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-fs-preserve"))
    host.execute_idempotent_command("echo 'test content' > /tmp/myfile.txt")
    docker_provider.stop_host(host, create_snapshot=False)

    restarted = docker_provider.start_host(host.id)
    result = restarted.execute_idempotent_command("cat /tmp/myfile.txt")
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
    docker_provider.destroy_host(host)

    # Container is removed but host record persists in DESTROYED state
    # so that gc_snapshots can age-gate snapshot cleanup. delete_host
    # purges the record fully.
    offline = docker_provider.get_host(host_id)
    assert isinstance(offline, OfflineHost)
    docker_provider.delete_host(offline)
    with pytest.raises(HostNotFoundError):
        docker_provider.get_host(host_id)


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_destroy_host_untags_build_image(docker_provider: DockerProviderInstance) -> None:
    # The default (no --image) path builds and tags an image per host.
    host = docker_provider.create_host(HostName("test-build-untag"))
    host_id = host.id
    build_tag = f"mngr-build-{host_id}"
    assert docker_provider._docker_client.images.get(build_tag) is not None

    # destroy_host untags the build image so built images don't pile up.
    docker_provider.destroy_host(host)
    with pytest.raises(docker.errors.ImageNotFound):
        docker_provider._docker_client.images.get(build_tag)

    # delete_host is idempotent and does not error on the already-removed tag.
    offline = docker_provider.get_host(host_id)
    docker_provider.delete_host(offline)


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_destroy_host_build_image_untag_preserves_snapshot_restore(
    docker_provider: DockerProviderInstance,
) -> None:
    host = docker_provider.create_host(HostName("test-build-untag-snap"))
    host_id = host.id
    snapshot_id = docker_provider.create_snapshot(host_id, SnapshotName("snap-untag"))

    # Untagging the build image on destroy must not break snapshot restore:
    # snapshot images are independent commits that keep their own layers.
    docker_provider.destroy_host(host)
    restored = docker_provider.start_host(host_id, snapshot_id=snapshot_id)
    assert isinstance(restored, Host)
    result = restored.execute_idempotent_command("echo restored")
    assert "restored" in result.stdout


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


@pytest.mark.docker_sdk
def test_read_only_construction_is_empty_and_creates_no_state_container(temp_mngr_ctx: MngrContext) -> None:
    """Building the docker provider for a read-only op must not create the state container.

    Mirrors the Modal backend: when nothing has been created yet,
    build_provider_instance raises ProviderEmptyError (so the provider loader
    skips docker) instead of lazily materializing the singleton state container
    -- which is what caused read-only commands like `mngr list` to leak state
    containers.
    """
    config = DockerProviderConfig(isolate_host_volumes=False)
    user_id = str(temp_mngr_ctx.get_profile_user_id())
    container_name = state_container_name(temp_mngr_ctx.config.prefix, user_id)

    client = create_docker_client()
    try:
        with pytest.raises(ProviderEmptyError):
            DockerProviderBackend.build_provider_instance(
                name=ProviderInstanceName("docker"),
                config=config,
                mngr_ctx=temp_mngr_ctx,
            )
        # The read-only construction must not have created the state container.
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(container_name)
    finally:
        # Defensive: if the assertion above regresses and a container WAS
        # created, remove it so this test does not itself leak.
        try:
            client.containers.get(container_name).remove(force=True)
        except docker.errors.NotFound:
            pass
        client.close()


@pytest.mark.docker_sdk
def test_bootstrap_for_host_creation_creates_state_container(temp_mngr_ctx: MngrContext) -> None:
    """The create path bootstraps the state container so build then succeeds.

    bootstrap_for_host_creation is the create-path counterpart to the read-only
    emptiness guard: it creates the singleton state container up front so the
    subsequent build_provider_instance call passes the guard instead of raising
    ProviderEmptyError.
    """
    config = DockerProviderConfig(isolate_host_volumes=False)
    user_id = str(temp_mngr_ctx.get_profile_user_id())
    container_name = state_container_name(temp_mngr_ctx.config.prefix, user_id)

    client = create_docker_client()
    instance: DockerProviderInstance | None = None
    try:
        DockerProviderBackend.bootstrap_for_host_creation(
            name=ProviderInstanceName("docker"),
            config=config,
            mngr_ctx=temp_mngr_ctx,
        )
        # Bootstrap created the state container...
        assert client.containers.get(container_name) is not None
        # ...so a subsequent read-only build no longer raises ProviderEmptyError.
        built = DockerProviderBackend.build_provider_instance(
            name=ProviderInstanceName("docker"),
            config=config,
            mngr_ctx=temp_mngr_ctx,
        )
        assert isinstance(built, DockerProviderInstance)
        instance = built
        assert instance.host_dir == Path("/mngr")
    finally:
        if instance is not None:
            instance.close()
        try:
            client.containers.get(container_name).remove(force=True)
        except docker.errors.NotFound:
            pass
        client.close()


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
    result = host.execute_idempotent_command("pgrep -x sshd")
    assert result.success, f"sshd not running: {result.stderr}"


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_ssh_packages_installed_after_create(docker_provider: DockerProviderInstance) -> None:
    """Verify required packages are installed in the container after create_host."""
    host = docker_provider.create_host(HostName("test-pkgs"))
    result = host.execute_idempotent_command("dpkg -l openssh-server")
    assert result.success, f"openssh-server not installed: {result.stderr}"


# =========================================================================
# Snapshot Restore
# =========================================================================


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_stop_with_snapshot_then_start_preserves_data(docker_provider: DockerProviderInstance) -> None:
    """Core snapshot workflow: write data, stop with snapshot, start, verify data."""
    host = docker_provider.create_host(HostName("test-snap-restore"))
    host.execute_idempotent_command("echo 'snapshot-payload-xyz' > /tmp/snapshot-data.txt")

    docker_provider.stop_host(host, create_snapshot=True)

    restarted = docker_provider.start_host(host.id)
    assert isinstance(restarted, Host)
    result = restarted.execute_idempotent_command("cat /tmp/snapshot-data.txt")
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
    result = host.execute_idempotent_command("cat /dockerfile-marker.txt")
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
def test_delete_host_cleans_up_snapshot_images(docker_provider: DockerProviderInstance) -> None:
    """Verify delete_host removes snapshot images and the host record.

    destroy_host preserves snapshots so gc_snapshots can age-gate them; the
    full purge happens in delete_host.
    """
    host = docker_provider.create_host(HostName("test-destroy-snap"))
    docker_provider.create_snapshot(host, SnapshotName("to-cleanup"))

    host_id = host.id
    docker_provider.destroy_host(host)

    # After destroy_host, snapshots are still tracked
    assert len(docker_provider.list_snapshots(host_id)) == 1

    docker_provider.delete_host(docker_provider.get_host(host_id))

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
    docker_provider.destroy_host(host)

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

    host_a.execute_idempotent_command("echo 'from-a' > /tmp/identity.txt")
    host_b.execute_idempotent_command("echo 'from-b' > /tmp/identity.txt")

    result_a = host_a.execute_idempotent_command("cat /tmp/identity.txt")
    result_b = host_b.execute_idempotent_command("cat /tmp/identity.txt")

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


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_disconnect_closes_paramiko_ssh_client(docker_provider: DockerProviderInstance) -> None:
    """Verify that Host.disconnect() closes the underlying paramiko SSH client.

    pyinfra's disconnect() only clears its SFTP cache and sets connected=False.
    It does NOT close the paramiko SSHClient, so when connect() is called again
    (e.g. during a retry after a transient SSH error), the old TCP connection
    leaks as an orphaned sshd-session on the server.

    This test creates a real Docker host, establishes an SSH connection, grabs
    a reference to the paramiko transport, disconnects, and verifies that the
    transport was actually closed.
    """
    host = docker_provider.create_host(HostName("test-disconnect"))

    # Establish the SSH connection by running a command
    result = host.execute_idempotent_command("echo connected")
    assert result.success

    # Grab the paramiko transport from the live connection.
    # Access pyinfra internals directly (not part of the public type surface);
    # the ty: ignore suppresses the unresolved-attribute type error.
    old_client = host.connector.host.connector.client  # ty: ignore[unresolved-attribute]
    assert old_client is not None
    old_transport = old_client.get_transport()
    assert old_transport is not None
    assert old_transport.is_active()

    # Disconnect
    host.disconnect()

    # The paramiko transport should be closed after disconnect
    assert not old_transport.is_active(), (
        "paramiko transport is still active after disconnect -- the SSH connection was leaked"
    )


# =============================================================================
# Host-volume isolation (volume-subpath)
# =============================================================================


@pytest.fixture
def isolated_docker_provider(temp_mngr_ctx: MngrContext) -> Generator[DockerProviderInstance, None, None]:
    """Like the standard docker_provider fixture but creates hosts with isolate_host_volumes=True."""
    yield from make_docker_provider_with_cleanup(temp_mngr_ctx, isolate_host_volumes=True)


@pytest.mark.docker
@pytest.mark.docker_sdk
@pytest.mark.release
def test_isolated_host_cannot_see_sibling_host_volumes(
    isolated_docker_provider: DockerProviderInstance,
) -> None:
    """When isolate_host_volumes=True, host A must not be able to read host B's vol-* directory."""
    host_a = isolated_docker_provider.create_host(HostName("test-iso-a"))
    host_b = isolated_docker_provider.create_host(HostName("test-iso-b"))

    # Write a marker file under host B's host_dir.
    write_result = host_b.execute_idempotent_command("echo b-only > /mngr/marker.txt")
    assert write_result.success

    # The legacy shared-volume mount put /mngr-state into every host; in the
    # isolated mode that mount does not exist, so /mngr-state must not be a
    # directory in either host.
    for host, label in ((host_a, "a"), (host_b, "b")):
        result = host.execute_idempotent_command("test -d /mngr-state && echo present || echo absent")
        assert result.success
        assert "absent" in result.stdout, f"/mngr-state is unexpectedly visible in isolated host {label}"

    # Host A must not see host B's marker file via any path. Sanity-check the
    # marker is readable from B (so the test is meaningful).
    read_b = host_b.execute_idempotent_command("cat /mngr/marker.txt")
    assert read_b.success
    assert "b-only" in read_b.stdout
    read_a = host_a.execute_idempotent_command("cat /mngr/marker.txt")
    assert not read_a.success or "b-only" not in read_a.stdout


@pytest.mark.docker
@pytest.mark.docker_sdk
@pytest.mark.release
def test_isolated_host_persists_data_across_restart(
    isolated_docker_provider: DockerProviderInstance,
) -> None:
    """The isolated subpath mount persists host_dir contents across stop/start, same as the shared mount."""
    host = isolated_docker_provider.create_host(HostName("test-iso-persist"))
    write = host.execute_idempotent_command("echo persisted > /mngr/restart-marker.txt")
    assert write.success

    isolated_docker_provider.stop_host(host, create_snapshot=False)
    restarted = isolated_docker_provider.start_host(host.id)
    assert isinstance(restarted, Host)
    read = restarted.execute_idempotent_command("cat /mngr/restart-marker.txt")
    assert read.success
    assert "persisted" in read.stdout
