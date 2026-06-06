import shlex
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.testing import created_host
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import SnapshotNotFoundError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_modal.errors import NoSnapshotsModalMngrError
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_modal.volume import ModalVolume
from imbue.mngr_recursive.provisioning import _upload_deploy_files

pytestmark = [pytest.mark.modal]


def _unique_host_name(prefix: str) -> HostName:
    """Build a per-test-unique host name.

    In shared-env offload mode many tests run concurrently against the same
    Modal environment, so reusing a fixed name (e.g. "test-host") risks
    name collisions -- especially for name-lookup tests. Suffix with a short
    random string so each test's host name is unique.
    """
    return HostName(f"{prefix}-{get_short_random_string()}")


def _make_agent_for_host(provider: ModalProviderInstance, host: Host) -> AgentInterface:
    """Build a minimal real ``BaseAgent`` bound to ``host`` for on_agent_created calls.

    ``ModalProviderInstance.on_agent_created`` only reads ``host`` (it creates the
    initial snapshot from the host's sandbox) and never touches the agent, but the
    signature requires a real ``AgentInterface``. Constructing a ``BaseAgent`` from
    the test's existing host (rather than passing ``None``) keeps the call type-safe
    without standing up any extra infrastructure. Construction is side-effect free.
    """
    return BaseAgent(
        id=AgentId.generate(),
        name=AgentName(f"test-agent-{get_short_random_string()}"),
        agent_type=AgentTypeName("command"),
        work_dir=host.host_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        host=host,
        mngr_ctx=provider.mngr_ctx,
        agent_config=AgentTypeConfig(command=CommandString("sleep 1000")),
    )


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_create_host_creates_sandbox_with_ssh(real_modal_provider: ModalProviderInstance) -> None:
    """Creating a host should create a Modal sandbox with SSH access."""
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
        # Verify host was created
        assert host.id is not None
        assert host.connector is not None

        # Verify SSH connector type
        assert host.connector.connector_cls_name == "SSHConnector"

        # Verify we can execute commands via SSH. This is the substantive
        # observable check that the sandbox was created and is reachable.
        result = host.execute_idempotent_command("echo 'hello from modal'")
        assert result.success
        assert "hello from modal" in result.stdout

        # NOTE: we deliberately do not assert on get_captured_output() here.
        # The captured build/creation output is environment-dependent (e.g. it
        # is empty when the image is already cached, so no build log is emitted),
        # so neither a non-empty nor a substring check is a reliable invariant on
        # this path. The delegation of get_captured_output() is covered directly
        # by test_modal_provider_app_get_captured_output in testing_provider_test.py.


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
    with created_host(persistent_modal_provider, _unique_host_name("test-host")) as host:
        # Verify host was created
        assert host.id is not None

        # Check that the shutdown script exists on the host
        result = host.execute_idempotent_command("test -f /mngr/commands/shutdown.sh && echo 'exists'")
        assert result.success
        assert "exists" in result.stdout

        # Verify the script content contains expected values
        result = host.execute_idempotent_command("cat /mngr/commands/shutdown.sh")
        assert result.success
        script_content = result.stdout

        # Check script has expected structure
        assert "#!/bin/bash" in script_content
        assert "curl" in script_content
        assert "snapshot_and_shutdown" in script_content or "modal.run" in script_content
        assert str(host.id) in script_content

        # Verify the script is executable
        result = host.execute_idempotent_command("test -x /mngr/commands/shutdown.sh && echo 'executable'")
        assert result.success
        assert "executable" in result.stdout


@pytest.mark.acceptance
@pytest.mark.flaky
@pytest.mark.timeout(300)
def test_get_host_by_id(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to get a host by its ID."""
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
        host_id = host.id

        # Get the same host by ID
        retrieved_host = real_modal_provider.get_host(host_id)
        assert retrieved_host.id == host_id


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_host_by_name(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to get a host by its name."""
    host_name = _unique_host_name("test-host")
    with created_host(real_modal_provider, host_name) as host:
        host_id = host.id

        # Get the same host by name
        retrieved_host = real_modal_provider.get_host(host_name)
        assert retrieved_host.id == host_id


@pytest.mark.acceptance
@pytest.mark.flaky
@pytest.mark.timeout(300)
def test_discover_hosts_includes_created_host(real_modal_provider: ModalProviderInstance) -> None:
    """Created host should appear in discover_hosts."""
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
        hosts = real_modal_provider.discover_hosts(cg=real_modal_provider.mngr_ctx.concurrency_group)
        host_ids = [h.host_id for h in hosts]
        assert host.id in host_ids


@pytest.mark.acceptance
@pytest.mark.flaky
@pytest.mark.timeout(300)
def test_destroy_host_stops_sandbox_and_delete_host_removes_record(
    real_modal_provider: ModalProviderInstance,
) -> None:
    """destroy_host stops the sandbox; delete_host removes the host record."""
    host = real_modal_provider.create_host(_unique_host_name("test-host"))
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
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
        resources = real_modal_provider.get_host_resources(host)

        assert resources.cpu.count >= 1
        assert resources.memory_gb >= 0.5


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_get_and_set_host_tags(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to get and set tags on a host."""
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
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


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_create_and_list_snapshots(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to create and list snapshots."""
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
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


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_list_snapshots_returns_initial_snapshot(initial_snapshot_provider: ModalProviderInstance) -> None:
    """list_snapshots should return the initial snapshot when is_snapshotted_after_create=True."""
    with created_host(initial_snapshot_provider, _unique_host_name("test-host")) as host:
        # we have to manually trigger the on_agent_created hook to create the initial snapshot (this is normally done automatically during the api::create_host call as a plugin callback)
        initial_snapshot_provider.on_agent_created(_make_agent_for_host(initial_snapshot_provider, host), host)
        snapshots = initial_snapshot_provider.list_snapshots(host)
        assert len(snapshots) == 1
        assert snapshots[0].name == "initial"


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_delete_snapshot(real_modal_provider: ModalProviderInstance) -> None:
    """Should be able to delete a snapshot."""
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
        # Initially no snapshots (is_snapshotted_after_create=False by default in tests)
        assert len(real_modal_provider.list_snapshots(host)) == 0

        # Create a snapshot
        snapshot_id = real_modal_provider.create_snapshot(host)
        assert len(real_modal_provider.list_snapshots(host)) == 1

        # Delete the created snapshot
        real_modal_provider.delete_snapshot(host, snapshot_id)
        # Should be back to no snapshots
        assert len(real_modal_provider.list_snapshots(host)) == 0


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_delete_nonexistent_snapshot_raises_error(real_modal_provider: ModalProviderInstance) -> None:
    """Deleting a nonexistent snapshot should raise SnapshotNotFoundError."""
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
        fake_id = SnapshotId("snap-nonexistent")
        with pytest.raises(SnapshotNotFoundError):
            real_modal_provider.delete_snapshot(host, fake_id)


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_start_host_restores_from_snapshot(real_modal_provider: ModalProviderInstance) -> None:
    """start_host with a snapshot_id should restore a terminated host from the snapshot."""
    host = None
    restored_host = None
    try:
        # Create a host and write a marker file
        host = real_modal_provider.create_host(_unique_host_name("test-host"))
        host_id = host.id

        # Write a marker file to verify restoration
        result = host.execute_idempotent_command("echo 'snapshot-marker' > /tmp/marker.txt")
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
        result = restored_host.execute_idempotent_command("cat /tmp/marker.txt")
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
    with created_host(real_modal_provider, _unique_host_name("test-host")) as host:
        host_id = host.id

        # Starting a running host should just return it
        started_host = real_modal_provider.start_host(host)
        assert started_host.id == host_id


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_restart_after_graceful_stop_uses_most_recent_snapshot(
    initial_snapshot_provider: ModalProviderInstance,
) -> None:
    """start_host on a gracefully-stopped host restores the MOST-RECENT snapshot, not the initial one.

    Uses initial_snapshot_provider (is_snapshotted_after_create=True), so there is
    an "initial" snapshot AND a "stop" snapshot created during stop_host(). This
    test pins which one a no-snapshot-id restart uses by writing a distinct marker
    before each snapshot:

    1. Write ``initial_marker`` and take the initial snapshot (via on_agent_created).
    2. Overwrite the same file with ``stop_marker``; stop_host() then captures the
       "stop" snapshot containing ``stop_marker``.
    3. Restart without a snapshot id and read the marker file back.

    If the most-recent (stop) snapshot is used, the file holds ``stop_marker``; if
    the initial snapshot were used instead it would hold ``initial_marker``. We
    assert the former, which is the documented restart behavior.
    """
    marker_path = "/tmp/restart_marker.txt"
    initial_marker = f"initial-{get_short_random_string()}"
    stop_marker = f"stop-{get_short_random_string()}"
    host = None
    restarted_host = None
    try:
        host = initial_snapshot_provider.create_host(_unique_host_name("test-host"))
        host_id = host.id

        # Write the initial-snapshot marker, then take the initial snapshot.
        result = host.execute_idempotent_command(f"echo '{initial_marker}' > {marker_path}")
        assert result.success
        # we have to manually trigger the on_agent_created hook to create the initial snapshot (this is normally done automatically during the api::create_host call as a plugin callback)
        initial_snapshot_provider.on_agent_created(_make_agent_for_host(initial_snapshot_provider, host), host)

        # Verify an initial snapshot was created
        snapshots = initial_snapshot_provider.list_snapshots(host)
        assert len(snapshots) == 1
        assert snapshots[0].name == "initial"

        # Overwrite the marker so the "stop" snapshot captures a different value.
        result = host.execute_idempotent_command(f"echo '{stop_marker}' > {marker_path}")
        assert result.success

        # Stop the host - this creates the more-recent "stop" snapshot.
        initial_snapshot_provider.stop_host(host)

        # Start it again without specifying a snapshot - should use the most recent snapshot.
        restarted_host = initial_snapshot_provider.start_host(host_id)

        # Verify the host was restarted with the same ID
        assert restarted_host.id == host_id

        # The restored marker proves which snapshot was used: the most-recent
        # "stop" snapshot (stop_marker), not the older "initial" one.
        result = restarted_host.execute_idempotent_command(f"cat {marker_path}")
        assert result.success
        assert stop_marker in result.stdout, (
            f"Expected the most-recent (stop) snapshot's marker '{stop_marker}' after restart, got: {result.stdout!r}"
        )
        assert initial_marker not in result.stdout, (
            f"Did not expect the initial snapshot's marker '{initial_marker}' after restart; "
            f"restart should use the most-recent snapshot. Got: {result.stdout!r}"
        )

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
        host_name = _unique_host_name("test-host")
        host = initial_snapshot_provider.create_host(host_name)
        host_id = host.id

        # we have to manually trigger the on_agent_created hook to create the initial snapshot (this is normally done automatically during the api::create_host call as a plugin callback)
        initial_snapshot_provider.on_agent_created(_make_agent_for_host(initial_snapshot_provider, host), host)

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
        result = restarted_host.execute_idempotent_command("echo 'restarted after hard kill'")
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
        host = real_modal_provider.create_host(_unique_host_name("test-host"))
        host_id = host.id

        # Verify NO initial snapshot was created
        snapshots = real_modal_provider.list_snapshots(host)
        assert len(snapshots) == 0

        # Write a marker file to verify snapshot state
        result = host.execute_idempotent_command("echo 'before-stop' > /tmp/marker.txt")
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
        result = restarted_host.execute_idempotent_command("cat /tmp/marker.txt")
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
    host_name = _unique_host_name("test-host")
    try:
        host = real_modal_provider.create_host(host_name)
        host_id = host.id

        # Verify NO initial snapshot was created
        snapshots = real_modal_provider.list_snapshots(host)
        assert len(snapshots) == 0

        # Hard kill: directly terminate the sandbox without using stop_host
        sandbox = real_modal_provider._find_sandbox_by_host_id(host_id)
        assert sandbox is not None
        sandbox.terminate()
        real_modal_provider._uncache_sandbox(host_id, host_name)

        # Should fail to restart because no snapshots exist
        with pytest.raises(NoSnapshotsModalMngrError):
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
    openssh-server tmux curl rsync git jq ca-certificates xxd \
    && rm -rf /var/lib/apt/lists/*
"""


def _write_offline_dockerfile(tmp_path: Path) -> Path:
    """Write the pre-configured Dockerfile for network-restricted tests."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(_OFFLINE_DOCKERFILE_CONTENT)
    return dockerfile


@pytest.mark.flaky
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
    with created_host(
        real_modal_provider,
        _unique_host_name("test-cidr"),
        build_args=[f"--file={dockerfile}", "--cidr-allowlist=192.0.2.0/24"],
    ) as host:
        # First confirm curl itself is present and runnable so a later non-zero
        # exit can be attributed to the network policy, not a missing binary.
        curl_check = host.execute_idempotent_command("command -v curl")
        assert curl_check.success, f"curl must be installed for this probe: {curl_check.stderr}"

        # curl to a public host should fail because it's outside the 192.0.2.0/24
        # (TEST-NET-1) allowlist. We capture curl's own exit code rather than
        # masking every failure with `|| echo blocked`, and assert it is one of
        # the network-reachability failure codes the policy produces:
        #   6  = could not resolve host (DNS egress blocked)
        #   7  = failed to connect (connection dropped/refused)
        #   28 = operation timed out (packets silently dropped, hit --max-time)
        # Any of these proves the request did not reach example.com. A 0 exit
        # would mean the restriction was silently not applied. We print the exit
        # code so the assertion message is meaningful.
        result = host.execute_idempotent_command(
            'curl -s --max-time 5 -o /dev/null https://example.com; echo "exit=$?"'
        )
        assert result.success
        assert any(f"exit={code}" in result.stdout for code in (6, 7, 28)), (
            f"Expected a network-block curl exit code (6/7/28) for an out-of-allowlist host, got: {result.stdout!r}"
        )


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_cidr_allowlist_allows_traffic_within_range(real_modal_provider: ModalProviderInstance) -> None:
    """A sandbox created with --cidr-allowlist=0.0.0.0/0 should allow all traffic.

    This is the complement of test_cidr_allowlist_restricts_network_access: it verifies
    that when the target IP is within the allowed CIDR range, traffic is not blocked.
    """
    with created_host(
        real_modal_provider,
        _unique_host_name("test-cidr-allow"),
        build_args=["--cidr-allowlist=0.0.0.0/0"],
    ) as host:
        # curl to a public IP should succeed because 0.0.0.0/0 allows everything
        result = host.execute_idempotent_command(
            "curl -s --max-time 10 -o /dev/null -w '%{http_code}' https://example.com"
        )
        assert result.success
        assert "200" in result.stdout


@pytest.mark.flaky
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
    with created_host(
        real_modal_provider,
        _unique_host_name("test-offline"),
        build_args=[f"--file={dockerfile}", "--offline"],
    ) as host:
        # Confirm curl is present so a non-zero exit reflects the network policy,
        # not a missing binary.
        curl_check = host.execute_idempotent_command("command -v curl")
        assert curl_check.success, f"curl must be installed for this probe: {curl_check.stderr}"

        # curl to a public host should fail because --offline blocks all outbound
        # traffic. We capture curl's own exit code instead of masking failures
        # with `|| echo blocked`, and assert it is one of the network-reachability
        # failure codes (see test_cidr_allowlist_restricts_network_access for the
        # code meanings): 6 (DNS blocked), 7 (connect failed), 28 (timed out).
        result = host.execute_idempotent_command(
            'curl -s --max-time 5 -o /dev/null https://example.com; echo "exit=$?"'
        )
        assert result.success
        assert any(f"exit={code}" in result.stdout for code in (6, 7, 28)), (
            f"Expected a network-block curl exit code (6/7/28) when offline, got: {result.stdout!r}"
        )


# =============================================================================
# Host Volume Tests
# =============================================================================


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_host_volume_is_symlinked_and_persists_data(real_modal_provider: ModalProviderInstance) -> None:
    """Host dir should be symlinked to the host volume, and data should persist on the volume."""
    with created_host(real_modal_provider, _unique_host_name("test-host-vol")) as host:
        # Verify /mngr is a symlink to /host_volume
        result = host.execute_idempotent_command("readlink /mngr")
        assert result.success
        assert "/host_volume" in result.stdout.strip()

        # Verify data written to /mngr lands on the volume
        result = host.execute_idempotent_command(
            "echo 'test data' > /mngr/test_file.txt && cat /host_volume/test_file.txt"
        )
        assert result.success
        assert "test data" in result.stdout

        # Verify the volume sync script is running
        result = host.execute_idempotent_command("test -f /mngr/commands/volume_sync.sh && echo 'exists'")
        assert result.success
        assert "exists" in result.stdout

        # Verify get_volume_for_host returns a volume. The volume name can take a
        # moment to become resolvable via Modal's control plane after the sandbox is
        # created (eventual consistency), so the name-lookup probe inside
        # get_volume_for_host may transiently return None right after creation. Poll
        # rather than asserting once.
        def volume_is_available() -> bool:
            return real_modal_provider.get_volume_for_host(host) is not None

        wait_for(volume_is_available, timeout=30.0, error_message="Host volume not visible after 30s")


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_host_volume_data_readable_via_volume_interface(real_modal_provider: ModalProviderInstance) -> None:
    """Data written inside the sandbox should be readable via the Volume interface from outside.

    Since Modal V2 volumes auto-commit writes, data written inside the sandbox
    should be visible via the Volume API from outside after a sync.
    """
    host = None
    try:
        host = real_modal_provider.create_host(_unique_host_name("test-vol-read"))

        # Write a known file and explicitly sync the volume
        host.execute_idempotent_command("echo 'volume test content' > /mngr/volume_test.txt && sync /host_volume")

        # The volume name can take a moment to become resolvable via Modal's control
        # plane after the sandbox is created (eventual consistency), so poll rather
        # than asserting once.
        wait_for(
            lambda: real_modal_provider.get_volume_for_host(host) is not None,
            timeout=30.0,
            error_message="Host volume not visible after 30s",
        )
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


# Wall-clock budget for the bulk upload itself (not host creation). rsync transfers
# 600 tiny files in a few seconds; a per-file SFTP upload would take ~0.7s/file (~400s),
# so this comfortably separates a single-rsync upload from a per-file regression while
# tolerating tunnel jitter.
_UPLOAD_BUDGET_SECONDS: Final[float] = 60.0


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(150)
def test_upload_deploy_files_handles_large_set_on_modal(
    real_modal_provider: ModalProviderInstance,
    tmp_path: Path,
) -> None:
    """Regression test for github issue 1825: a large deploy-file set must upload via one rsync.

    Uploading each deploy file through its own SFTP channel is a full round-trip over
    the Modal SSH tunnel (~0.7s/file measured), so a real user's ``~/.claude/plugins``
    tree (hundreds of files) would blow past the upload timeout or reset the connection
    ("Error reading SSH protocol banner"). The whole set must instead transfer with a
    single ``host.copy_local_directory`` (rsync) call.

    The real guard is the explicit ``_UPLOAD_BUDGET_SECONDS`` assertion on the upload
    call itself, decoupled from (variable) host-creation time: 600 files would take
    ~400s via a per-file path but only a few seconds via rsync. The mix of on-disk
    ``Path`` sources and in-memory string content exercises both staging branches; we
    then verify every file landed on the remote with the expected contents.
    """
    file_count = 600
    with created_host(real_modal_provider, HostName("rsync-deploy-test")) as host:
        home_result = host.execute_idempotent_command("echo $HOME")
        assert home_result.success
        remote_home = home_result.stdout.strip()

        # Pre-existing file in a target dir that is NOT in the upload set: the rsync
        # transfer must be additive (no --delete), so it must survive.
        sentinel = f"{remote_home}/.mngr/deploytest/sub0/preexisting.txt"
        host.execute_idempotent_command(f"mkdir -p {shlex.quote(remote_home)}/.mngr/deploytest/sub0")
        host.write_text_file(Path(sentinel), "do-not-delete")

        deploy_files: dict[Path, Path | str] = {}
        for i in range(file_count):
            dest = Path(f"~/.mngr/deploytest/sub{i % 20}/file_{i}.txt")
            if i % 2 == 0:
                source_file = tmp_path / f"src_{i}.txt"
                source_file.write_text(f"path-content-{i}")
                deploy_files[dest] = source_file
            else:
                deploy_files[dest] = f"str-content-{i}"

        start = time.monotonic()
        uploaded = _upload_deploy_files(host, deploy_files, remote_home)
        upload_elapsed = time.monotonic() - start
        assert uploaded == file_count
        assert upload_elapsed < _UPLOAD_BUDGET_SECONDS, (
            f"deploy-file upload took {upload_elapsed:.1f}s for {file_count} files "
            f"(budget {_UPLOAD_BUDGET_SECONDS:.0f}s) -- per-file-upload regression?"
        )

        # Every uploaded file plus the pre-existing sentinel must be present (rsync is
        # additive: the sentinel, absent from the upload set, must not be deleted).
        remote_dir = f"{remote_home}/.mngr/deploytest"
        count_result = host.execute_idempotent_command(f"find {shlex.quote(remote_dir)} -type f | wc -l")
        assert count_result.success
        assert int(count_result.stdout.strip()) == file_count + 1
        assert host.read_text_file(Path(sentinel)) == "do-not-delete"

        # Spot-check one Path-sourced and one string-sourced file's contents.
        assert host.read_text_file(Path(remote_home) / ".mngr/deploytest/sub0/file_0.txt") == "path-content-0"
        assert host.read_text_file(Path(remote_home) / ".mngr/deploytest/sub1/file_1.txt") == "str-content-1"
