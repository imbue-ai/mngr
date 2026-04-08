"""Tests for Docker host state transitions as observed through discover_hosts().

Verifies that the host_state field on DiscoveredHost is correct at each
lifecycle stage: RUNNING after create, STOPPED after stop, RUNNING after
restart, CRASHED after container kill, and FAILED for bad builds.

These tests exercise real Docker containers (except the FAILED test which
only writes a host record) to ensure the full discovery -> state derivation
pipeline works end-to-end.
"""

from collections.abc import Generator

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_docker_provider_with_cleanup

pytestmark = [pytest.mark.timeout(120)]


@pytest.fixture
def docker_provider(temp_mngr_ctx: MngrContext) -> Generator[DockerProviderInstance, None, None]:
    yield from make_docker_provider_with_cleanup(temp_mngr_ctx)


def _find_host_state(hosts: list[DiscoveredHost], host_id: HostId) -> HostState | None:
    """Find a host's discovered state by ID, or None if not found."""
    for h in hosts:
        if h.host_id == host_id:
            return h.host_state
    return None


# =========================================================================
# Acceptance tests -- run on every branch in CI
# =========================================================================


@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_host_state_running_after_create(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """After create_host, discover_hosts should report host_state=RUNNING."""
    host = docker_provider.create_host(HostName("test-state-running"))

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    state = _find_host_state(hosts, host.id)

    assert state == HostState.RUNNING


@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_host_state_stopped_after_stop(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """After stop_host, discover_hosts should report host_state=STOPPED."""
    host = docker_provider.create_host(HostName("test-state-stopped"))
    docker_provider.stop_host(host, create_snapshot=False)

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    state = _find_host_state(hosts, host.id)

    assert state == HostState.STOPPED


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_host_state_running_after_restart(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """After stop then start, discover_hosts should report host_state=RUNNING."""
    host = docker_provider.create_host(HostName("test-state-restart"))
    docker_provider.stop_host(host, create_snapshot=False)
    docker_provider.start_host(host.id)

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    state = _find_host_state(hosts, host.id)

    assert state == HostState.RUNNING


@pytest.mark.acceptance
@pytest.mark.docker_sdk
def test_discover_host_state_failed_for_bad_build(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A host with failure_reason should appear in discovery with host_state=FAILED.

    This test does not create a real container -- it writes a failed host record
    directly to the host store, which is the same thing create_host does when a
    build fails.
    """
    host_id = HostId.generate()
    docker_provider._save_failed_host_record(
        host_id=host_id,
        host_name=HostName("test-state-failed"),
        tags={},
        failure_reason="Intentional test failure",
        build_log="",
    )

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    state = _find_host_state(hosts, host_id)

    assert state == HostState.FAILED


# =========================================================================
# Release tests -- run on release branch only
# =========================================================================


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_host_state_crashed_after_container_kill(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """After a container is killed (simulating a crash), discovery should report CRASHED.

    When a container is killed, stop_host is never called, so stop_reason stays
    None. derive_offline_host_state with stop_reason=None and
    supports_shutdown_hosts=True returns CRASHED.
    """
    host = docker_provider.create_host(HostName("test-state-crashed"))

    container = docker_provider._find_container_by_host_id(host.id)
    assert container is not None
    container.kill()
    # Wait for Docker to update container status after kill
    container.wait()

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    state = _find_host_state(hosts, host.id)

    assert state == HostState.CRASHED


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_stopped_host_with_snapshot_visible(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """After stop_host(create_snapshot=True), host should be STOPPED and have a snapshot."""
    host = docker_provider.create_host(HostName("test-state-snap-stop"))
    docker_provider.stop_host(host, create_snapshot=True)

    hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    state = _find_host_state(hosts, host.id)
    assert state == HostState.STOPPED

    snapshots = docker_provider.list_snapshots(host.id)
    assert len(snapshots) >= 1
    assert any(str(s.name).startswith("stop-") for s in snapshots)


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_discover_destroyed_host_excluded_by_default_included_with_flag(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """After destroy_host, host is excluded from default discovery but included with include_destroyed.

    Docker's destroy_host calls stop_host first (setting stop_reason=STOPPED),
    then removes the container. With delete_snapshots=False the host record is
    kept. In discovery with include_destroyed=True, the host appears with
    host_state=STOPPED (not DESTROYED, because Docker supports shutdown so
    derive_offline_host_state returns HostState(stop_reason)).
    """
    host = docker_provider.create_host(HostName("test-state-destroyed"))
    host_id = host.id
    docker_provider.destroy_host(host, delete_snapshots=False)

    # Default discovery should exclude the host
    hosts_default = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    assert _find_host_state(hosts_default, host_id) is None

    # include_destroyed=True should include it
    hosts_all = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group, include_destroyed=True)
    state = _find_host_state(hosts_all, host_id)
    assert state == HostState.STOPPED


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_full_lifecycle_state_transitions(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Walk through every reachable Docker state transition and verify discovery at each step."""
    cg = temp_mngr_ctx.concurrency_group

    # 1. Create -> RUNNING
    host = docker_provider.create_host(HostName("test-lifecycle"))
    host_id = host.id
    assert _find_host_state(docker_provider.discover_hosts(cg), host_id) == HostState.RUNNING

    # 2. Stop with snapshot -> STOPPED
    docker_provider.stop_host(host, create_snapshot=True)
    assert _find_host_state(docker_provider.discover_hosts(cg), host_id) == HostState.STOPPED

    # 3. Start -> RUNNING
    docker_provider.start_host(host_id)
    assert _find_host_state(docker_provider.discover_hosts(cg), host_id) == HostState.RUNNING

    # 4. Stop without snapshot -> STOPPED
    docker_provider.stop_host(host_id, create_snapshot=False)
    assert _find_host_state(docker_provider.discover_hosts(cg), host_id) == HostState.STOPPED

    # 5. Start again -> RUNNING
    docker_provider.start_host(host_id)
    assert _find_host_state(docker_provider.discover_hosts(cg), host_id) == HostState.RUNNING

    # 6. Destroy -> absent from default discovery
    docker_provider.destroy_host(host_id, delete_snapshots=True)
    assert _find_host_state(docker_provider.discover_hosts(cg), host_id) is None
