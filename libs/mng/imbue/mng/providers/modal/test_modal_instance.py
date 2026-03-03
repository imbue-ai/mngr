from pathlib import Path

import pytest

from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import SnapshotNotFoundError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.volume import HostVolume
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.providers.modal.errors import NoSnapshotsModalMngError
from imbue.mng.providers.modal.instance import ModalProviderInstance
from imbue.mng.providers.modal.volume import ModalVolume
from imbue.mng.utils.polling import wait_for

# Placeholder for the agent parameter in on_agent_created calls.
# The method doesn't use the agent, but the type signature requires AgentInterface.
_UNUSED_AGENT: AgentInterface = None  # ty: ignore[invalid-assignment]


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_create_host_creates_sandbox_with_ssh(real_modal_provider: ModalProviderInstance) -> None:
    """Creating a host should create a Modal sandbox with SSH access."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))

        # Verify host was created
        assert host.id is not None
        assert host.connector is not None

        # Verify SSH connector type
        assert host.connector.connector_cls_name == "SSHConnector"

        # Verify we can execute commands via SSH
        result = host.execute_command("echo 'hello from modal'")
        assert result.success
        assert "hello from modal" in result.stdout

        # Verify output capture is working (Modal should emit some output during host creation)
        captured_output = real_modal_provider.get_captured_output()
        assert isinstance(captured_output, str)

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_persistent_host_creates_shutdown_script(
    persistent_modal_provider: ModalProviderInstance,
) -> None:
    """Persistent Modal host should have a shutdown script created.

    This test verifies that when using a persistent Modal app (is_persistent=True),
    the snapshot_and_shutdown function is deployed and a shutdown script is written
    to the host at <host_dir>/commands/shutdown.sh.
    """
    host = None
    try:
        host = persistent_modal_provider.create_host(HostName("test-host"))

        # Verify host was created
        assert host.id is not None

        # Check that the shutdown script exists on the host
        result = host.execute_command("test -f /mng/commands/shutdown.sh && echo 'exists'")
        assert result.success
        assert "exists" in result.stdout

        # Verify the script content contains expected values
        result = host.execute_command("cat /mng/commands/shutdown.sh")
        assert result.success
        script_content = result.stdout

        # Check script has expected structure
        assert "#!/bin/bash" in script_content
        assert "curl" in script_content
        assert "snapshot_and_shutdown" in script_content or "modal.run" in script_content
        assert str(host.id) in script_content

        # Verify the script is executable
        result = host.execute_command("test -x /mng/commands/shutdown.sh && echo 'executable'")
        assert result.success
        assert "executable" in result.stdout

    finally:
        if host:
            persistent_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_host_by_id(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to get a host by its ID."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))
        host_id = host.id

        # Get the same host by ID
        retrieved_host = real_modal_provider.get_host(host_id)
        assert retrieved_host.id == host_id

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_host_by_name(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to get a host by its name."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))
        host_id = host.id

        # Get the same host by name
        retrieved_host = real_modal_provider.get_host(HostName("test-host"))
        assert retrieved_host.id == host_id

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_list_hosts_includes_created_host(real_modal_provider: ModalProviderInstance) -> None:
    """Created host should appear in list_hosts."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))

        hosts = real_modal_provider.list_hosts(cg=real_modal_provider.mng_ctx.concurrency_group)
        host_ids = [h.id for h in hosts]
        assert host.id in host_ids

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_destroy_host_stops_sandbox_and_delete_host_removes_record(
    real_modal_provider: ModalProviderInstance,
) -> None:
    """destroy_host stops the sandbox; delete_host removes the host record."""
    host = real_modal_provider.create_host(HostName("test-host"))
    host_id = host.id

    try:
        real_modal_provider.destroy_host(host)

        # Host record still exists (as an offline host) after destroy
        found_host = real_modal_provider.get_host(host_id)
        assert found_host.id == host_id

        # delete_host permanently removes the record
        real_modal_provider.delete_host(found_host)

        with pytest.raises(HostNotFoundError):
            real_modal_provider.get_host(host_id)
    finally:
        real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_host_resources(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to get resource information for a host."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))
        resources = real_modal_provider.get_host_resources(host)

        assert resources.cpu.count >= 1
        assert resources.memory_gb >= 0.5

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_and_set_host_tags(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to get and set tags on a host."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))

        # Initially no tags
        tags = real_modal_provider.get_host_tags(host)
        assert tags == {}

        # Set some tags
        real_modal_provider.set_host_tags(host, {"env": "test", "team": "backend"})
        tags = real_modal_provider.get_host_tags(host)
        assert tags == {"env": "test", "team": "backend"}

        # Add a tag
        real_modal_provider.add_tags_to_host(host, {"version": "1.0"})
        tags = real_modal_provider.get_host_tags(host)
        assert len(tags) == 3
        assert tags["version"] == "1.0"

        # Remove a tag
        real_modal_provider.remove_tags_from_host(host, ["team"])
        tags = real_modal_provider.get_host_tags(host)
        assert "team" not in tags
        assert len(tags) == 2

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_create_and_list_snapshots(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to create and list snapshots."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))

        # Initially there are no snapshots (is_snapshotted_after_create=False by default in tests)
        snapshots = real_modal_provider.list_snapshots(host)
        assert len(snapshots) == 0

        # Create a snapshot
        snapshot_id = real_modal_provider.create_snapshot(host, SnapshotName("test-snapshot"))
        assert snapshot_id is not None

        # Verify it appears in the list
        snapshots = real_modal_provider.list_snapshots(host)
        assert len(snapshots) == 1
        assert snapshots[0].id == snapshot_id
        assert snapshots[0].name == SnapshotName("test-snapshot")
        assert snapshots[0].recency_idx == 0

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_list_snapshots_returns_initial_snapshot(initial_snapshot_provider: ModalProviderInstance) -> None:
    """list_snapshots should return the initial snapshot when is_snapshotted_after_create=True."""
    host = None
    try:
        host = initial_snapshot_provider.create_host(HostName("test-host"))
        # we have to manually trigger the on_agent_created hook to create the initial snapshot (this is normally done automatically during the api::create_host call as a plugin callback)
        initial_snapshot_provider.on_agent_created(_UNUSED_AGENT, host)
        snapshots = initial_snapshot_provider.list_snapshots(host)
        assert len(snapshots) == 1
        assert snapshots[0].name == "initial"

    finally:
        if host:
            initial_snapshot_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_delete_snapshot(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to delete a snapshot."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))

        # Initially no snapshots (is_snapshotted_after_create=False by default in tests)
        assert len(real_modal_provider.list_snapshots(host)) == 0

        # Create a snapshot
        snapshot_id = real_modal_provider.create_snapshot(host)
        assert len(real_modal_provider.list_snapshots(host)) == 1

        # Delete the created snapshot
        real_modal_provider.delete_snapshot(host, snapshot_id)
        # Should be back to no snapshots
        assert len(real_modal_provider.list_snapshots(host)) == 0

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_delete_nonexistent_snapshot_raises_error(real_modal_provider: ModalProviderInstance) -> None:
    """Deleting a nonexistent snapshot should raise SnapshotNotFoundError."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))

        fake_id = SnapshotId("snap-nonexistent")
        with pytest.raises(SnapshotNotFoundError):
            real_modal_provider.delete_snapshot(host, fake_id)

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_start_host_restores_from_snapshot(real_modal_provider: ModalProviderInstance) -> None:
    """start_host with a snapshot_id should restore a terminated host from the snapshot."""
    host = None
    restored_host = None
    try:
        # Create a host and write a marker file
        host = real_modal_provider.create_host(HostName("test-host"))
        host_id = host.id

        # Write a marker file to verify restoration
        result = host.execute_command("echo 'snapshot-marker' > /tmp/marker.txt")
        assert result.success

        # Create a snapshot
        snapshot_id = real_modal_provider.create_snapshot(host, SnapshotName("test-restore"))

        # Verify snapshot exists
        snapshots = real_modal_provider.list_snapshots(host)
        assert len(snapshots) == 1
        assert snapshots[0].id == snapshot_id

        # Stop the host (terminates the sandbox)
        real_modal_provider.stop_host(host)

        # Restore from snapshot
        restored_host = real_modal_provider.start_host(host_id, snapshot_id=snapshot_id)

        # Verify the host was restored with the same ID
        assert restored_host.id == host_id

        # Verify the marker file exists (proving we restored from snapshot)
        result = restored_host.execute_command("cat /tmp/marker.txt")
        assert result.success
        assert "snapshot-marker" in result.stdout

    finally:
        if restored_host:
            real_modal_provider.destroy_host(restored_host)
        elif host:
            real_modal_provider.destroy_host(host)
        else:
            pass


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_start_host_on_running_host(real_modal_provider: ModalProviderInstance) -> None:
    """start_host on a running host should return the same host."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))
        host_id = host.id

        # Starting a running host should just return it
        started_host = real_modal_provider.start_host(host)
        assert started_host.id == host_id

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_start_host_on_stopped_host_uses_initial_snapshot(initial_snapshot_provider: ModalProviderInstance) -> None:
    """start_host on a terminated host should restart from the initial snapshot.

    This test uses initial_snapshot_provider (is_snapshotted_after_create=True) to
    verify that hosts can be restarted using the initial snapshot.
    """
    host = None
    restarted_host = None
    try:
        host = initial_snapshot_provider.create_host(HostName("test-host"))
        host_id = host.id

        # we have to manually trigger the on_agent_created hook to create the initial snapshot (this is normally done automatically during the api::create_host call as a plugin callback)
        initial_snapshot_provider.on_agent_created(_UNUSED_AGENT, host)

        # Verify an initial snapshot was created
        snapshots = initial_snapshot_provider.list_snapshots(host)
        assert len(snapshots) == 1
        assert snapshots[0].name == "initial"

        # Stop the host (this will also create a "stop" snapshot, but we ignore it)
        initial_snapshot_provider.stop_host(host)

        # Start it again without specifying a snapshot - should use most recent snapshot
        restarted_host = initial_snapshot_provider.start_host(host_id)

        # Verify the host was restarted with the same ID
        assert restarted_host.id == host_id

        # Verify we can execute commands on the restarted host
        result = restarted_host.execute_command("echo 'restarted successfully'")
        assert result.success
        assert "restarted successfully" in result.stdout

    finally:
        if restarted_host:
            initial_snapshot_provider.destroy_host(restarted_host)
        elif host:
            initial_snapshot_provider.destroy_host(host)
        else:
            pass


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_host_not_found_raises_error(real_modal_provider: ModalProviderInstance) -> None:
    """Getting a non-existent host should raise HostNotFoundError."""
    fake_id = HostId.generate()
    with pytest.raises(HostNotFoundError):
        real_modal_provider.get_host(fake_id)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_host_by_name_not_found_raises_error(real_modal_provider: ModalProviderInstance) -> None:
    """Getting a non-existent host by name should raise HostNotFoundError."""
    with pytest.raises(HostNotFoundError):
        real_modal_provider.get_host(HostName("nonexistent-host"))


# =============================================================================
# Tests for is_snapshotted_after_create configuration
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_restart_after_hard_kill_with_initial_snapshot(initial_snapshot_provider: ModalProviderInstance) -> None:
    """Host can restart after hard kill when initial snapshot is enabled.

    This tests scenario 1: is_snapshotted_after_create=True.
    Even if the sandbox is terminated directly (hard kill), the host should be
    restartable because an initial snapshot exists.
    """
    host = None
    restarted_host = None
    try:
        host = initial_snapshot_provider.create_host(HostName("test-host"))
        host_id = host.id
        host_name = HostName("test-host")

        # we have to manually trigger the on_agent_created hook to create the initial snapshot (this is normally done automatically during the api::create_host call as a plugin callback)
        initial_snapshot_provider.on_agent_created(_UNUSED_AGENT, host)

        # Verify initial snapshot was created
        snapshots = initial_snapshot_provider.list_snapshots(host)
        assert len(snapshots) == 1
        assert snapshots[0].name == "initial"

        # Hard kill: directly terminate the sandbox without using stop_host
        sandbox = initial_snapshot_provider._find_sandbox_by_host_id(host_id)
        assert sandbox is not None
        sandbox.terminate()
        initial_snapshot_provider._uncache_sandbox(host_id, host_name)

        # Should be able to restart using the initial snapshot
        restarted_host = initial_snapshot_provider.start_host(host_id)
        assert restarted_host.id == host_id

        # Verify the host is functional
        result = restarted_host.execute_command("echo 'restarted after hard kill'")
        assert result.success
        assert "restarted after hard kill" in result.stdout

    finally:
        if restarted_host:
            initial_snapshot_provider.destroy_host(restarted_host)
        elif host:
            initial_snapshot_provider.destroy_host(host)
        else:
            pass


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_restart_after_graceful_stop_without_initial_snapshot(
    real_modal_provider: ModalProviderInstance,
) -> None:
    """Host can restart after graceful stop even without initial snapshot.

    This tests scenario 2: is_snapshotted_after_create=False (the test default).
    When the host is stopped gracefully via stop_host(), a snapshot is created
    during the stop process, allowing the host to be restarted.
    """
    host = None
    restarted_host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))
        host_id = host.id

        # Verify NO initial snapshot was created
        snapshots = real_modal_provider.list_snapshots(host)
        assert len(snapshots) == 0

        # Write a marker file to verify snapshot state
        result = host.execute_command("echo 'before-stop' > /tmp/marker.txt")
        assert result.success

        # Graceful stop - should create a snapshot
        real_modal_provider.stop_host(host_id, create_snapshot=True)

        # Verify snapshot was created during stop
        snapshots = real_modal_provider.list_snapshots(host_id)
        assert len(snapshots) == 1
        assert snapshots[0].name == "stop"

        # Should be able to restart
        restarted_host = real_modal_provider.start_host(host_id)
        assert restarted_host.id == host_id

        # Verify the marker file exists (state was preserved)
        result = restarted_host.execute_command("cat /tmp/marker.txt")
        assert result.success
        assert "before-stop" in result.stdout

    finally:
        if restarted_host:
            real_modal_provider.destroy_host(restarted_host)
        elif host:
            real_modal_provider.destroy_host(host)
        else:
            pass


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_restart_fails_after_hard_kill_without_initial_snapshot(
    real_modal_provider: ModalProviderInstance,
) -> None:
    """Host cannot restart after hard kill when no initial snapshot exists.

    This tests scenario 3: is_snapshotted_after_create=False (the test default) + hard kill.
    When the sandbox is terminated directly without stop_host() being called,
    no snapshot exists, and the host cannot be restarted.
    """
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host"))
        host_id = host.id
        host_name = HostName("test-host")

        # Verify NO initial snapshot was created
        snapshots = real_modal_provider.list_snapshots(host)
        assert len(snapshots) == 0

        # Hard kill: directly terminate the sandbox without using stop_host
        sandbox = real_modal_provider._find_sandbox_by_host_id(host_id)
        assert sandbox is not None
        sandbox.terminate()
        real_modal_provider._uncache_sandbox(host_id, host_name)

        # Should fail to restart because no snapshots exist
        with pytest.raises(NoSnapshotsModalMngError):
            real_modal_provider.start_host(host_id)

    finally:
        # Host record still exists on the volume, so clean up
        if host:
            real_modal_provider._delete_host_record(host.id)


# =============================================================================
# Network restriction tests
# =============================================================================

# Dockerfile with all packages pre-installed for network-restricted tests.
# When --offline or restrictive --cidr-allowlist is used, the sandbox cannot
# apt-get install packages at runtime, so everything must be baked into the image.
_OFFLINE_DOCKERFILE_CONTENT = """\
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server tmux curl rsync git jq \
    && rm -rf /var/lib/apt/lists/*
"""


def _write_offline_dockerfile(tmp_path: Path) -> Path:
    """Write the pre-configured Dockerfile for network-restricted tests."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(_OFFLINE_DOCKERFILE_CONTENT)
    return dockerfile


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_cidr_allowlist_restricts_network_access(real_modal_provider: ModalProviderInstance, tmp_path: Path) -> None:
    """A sandbox created with --cidr-allowlist should block traffic to IPs outside the allowed ranges.

    Creates a sandbox allowing only 192.0.2.0/24 (TEST-NET-1, not routable), then
    verifies that an outbound HTTP request to a public IP fails.

    Uses a pre-built image because the sandbox cannot apt-get install packages
    when outbound network is restricted.
    """
    dockerfile = _write_offline_dockerfile(tmp_path)
    host = None
    try:
        host = real_modal_provider.create_host(
            HostName("test-cidr"),
            build_args=[f"--file={dockerfile}", "--cidr-allowlist=192.0.2.0/24"],
        )

        # curl to a public IP should fail because it's outside the allowlist
        result = host.execute_command(
            "curl -s --max-time 5 -o /dev/null -w '%{http_code}' https://example.com || echo 'blocked'"
        )
        assert result.success
        assert "blocked" in result.stdout

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_cidr_allowlist_allows_traffic_within_range(real_modal_provider: ModalProviderInstance) -> None:
    """A sandbox created with --cidr-allowlist=0.0.0.0/0 should allow all traffic.

    This is the complement of test_cidr_allowlist_restricts_network_access: it verifies
    that when the target IP is within the allowed CIDR range, traffic is not blocked.
    """
    host = None
    try:
        host = real_modal_provider.create_host(
            HostName("test-cidr-allow"),
            build_args=["--cidr-allowlist=0.0.0.0/0"],
        )

        # curl to a public IP should succeed because 0.0.0.0/0 allows everything
        result = host.execute_command("curl -s --max-time 10 -o /dev/null -w '%{http_code}' https://example.com")
        assert result.success
        assert "200" in result.stdout

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_offline_blocks_all_network_access(real_modal_provider: ModalProviderInstance, tmp_path: Path) -> None:
    """A sandbox created with --offline should block all outbound network traffic.

    Uses an empty cidr_allowlist under the hood, which Modal interprets as
    'no CIDRs allowed' = block all outbound traffic.

    Uses a pre-built image because the sandbox cannot apt-get install packages
    when outbound network is blocked.
    """
    dockerfile = _write_offline_dockerfile(tmp_path)
    host = None
    try:
        host = real_modal_provider.create_host(
            HostName("test-offline"),
            build_args=[f"--file={dockerfile}", "--offline"],
        )

        # curl to a public IP should fail because all outbound traffic is blocked
        result = host.execute_command(
            "curl -s --max-time 5 -o /dev/null -w '%{http_code}' https://example.com || echo 'blocked'"
        )
        assert result.success
        assert "blocked" in result.stdout

    finally:
        if host:
            real_modal_provider.destroy_host(host)


# =============================================================================
# Host Volume Tests
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_host_volume_is_symlinked_and_persists_data(real_modal_provider: ModalProviderInstance) -> None:
    """Host dir should be symlinked to the host volume, and data should persist on the volume."""
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-host-vol"))

        # Verify /mng is a symlink to /host_volume
        result = host.execute_command("readlink /mng")
        assert result.success
        assert "/host_volume" in result.stdout.strip()

        # Verify data written to /mng lands on the volume
        result = host.execute_command("echo 'test data' > /mng/test_file.txt && cat /host_volume/test_file.txt")
        assert result.success
        assert "test data" in result.stdout

        # Verify the volume sync script is running
        result = host.execute_command("test -f /mng/commands/volume_sync.sh && echo 'exists'")
        assert result.success
        assert "exists" in result.stdout

        # Verify get_volume_for_host returns a volume
        volume = real_modal_provider.get_volume_for_host(host)
        assert volume is not None

    finally:
        if host:
            real_modal_provider.destroy_host(host)


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_host_volume_data_readable_via_volume_interface(real_modal_provider: ModalProviderInstance) -> None:
    """Data written inside the sandbox should be readable via the Volume interface from outside.

    Since Modal V2 volumes auto-commit writes, data written inside the sandbox
    should be visible via the Volume API from outside after a sync.
    """
    host = None
    try:
        host = real_modal_provider.create_host(HostName("test-vol-read"))

        # Write a known file and explicitly sync the volume
        host.execute_command("echo 'volume test content' > /mng/volume_test.txt && sync /host_volume")

        host_volume = real_modal_provider.get_volume_for_host(host)
        assert host_volume is not None
        assert isinstance(host_volume, HostVolume)
        assert isinstance(host_volume.volume, ModalVolume)

        # Poll until the file is visible (auto-commit may take a moment)
        def file_is_readable() -> bool:
            try:
                content = host_volume.volume.read_file("/volume_test.txt")
                return b"volume test content" in content
            except FileNotFoundError:
                return False

        wait_for(file_is_readable, timeout=30.0, error_message="Volume file not visible after 30s")

    finally:
        if host:
            real_modal_provider.destroy_host(host)

            # Verify the volume is gone
            volume_after = real_modal_provider.get_volume_for_host(host.id)
            assert volume_after is None
