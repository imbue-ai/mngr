"""Unit tests for the imbue_cloud provider instance helpers."""

from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.instance import ImbueCloudProvider
from imbue.mngr_imbue_cloud.instance import _map_docker_status_to_host_state
from imbue.mngr_imbue_cloud.primitives import LeaseDbId


@pytest.mark.parametrize(
    "status,exit_code,expected_state",
    [
        # Running container with unreachable inner SSH should report as
        # UNAUTHENTICATED (host is up; we just can't get inside).
        ("running", 0, HostState.UNAUTHENTICATED),
        # exit_code is ignored when running.
        ("running", 137, HostState.UNAUTHENTICATED),
        # Cleanly-exited containers map to STOPPED.
        ("exited", 0, HostState.STOPPED),
        # Non-zero exit means the container crashed.
        ("exited", 1, HostState.CRASHED),
        ("exited", 137, HostState.CRASHED),
        # Paused containers preserve their PAUSED state.
        ("paused", 0, HostState.PAUSED),
        # In-progress lifecycle states render as STARTING so the user knows
        # to wait, not assume the host is broken.
        ("created", 0, HostState.STARTING),
        ("restarting", 0, HostState.STARTING),
        # Terminal-but-broken docker states surface as CRASHED.
        ("dead", 0, HostState.CRASHED),
        ("removing", 0, HostState.CRASHED),
        # Unknown statuses default to CRASHED so we never silently misreport.
        ("nonsense", 0, HostState.CRASHED),
        ("", 0, HostState.CRASHED),
    ],
)
def test_map_docker_status_to_host_state(status: str, exit_code: int, expected_state: HostState) -> None:
    state, note = _map_docker_status_to_host_state(status, exit_code)
    assert state == expected_state
    # Every mapping returns a non-empty diagnostic note that gets folded
    # into HostDetails.failure_reason; assert it's at least populated so
    # the user sees *something* in the listing.
    assert note is not None
    assert note != ""


def test_map_docker_status_running_note_mentions_inner_ssh() -> None:
    """The running-but-unreachable case must explain why we landed there."""
    _state, note = _map_docker_status_to_host_state("running", 0)
    assert note is not None
    assert "inner SSH" in note


def test_map_docker_status_exited_nonzero_note_includes_exit_code() -> None:
    """A crashed container's note should surface the exit code for debugging."""
    _state, note = _map_docker_status_to_host_state("exited", 137)
    assert note is not None
    assert "137" in note


class _StubImbueCloudProvider(ImbueCloudProvider):
    """Test stub that supplies a tmp keypair path so we don't hit real disk paths."""

    _stub_keypair_dir: Path = Path("/tmp/stub-imbue-cloud-keypair")

    def _host_keypair_paths(self, host_id: HostId) -> tuple[Path, Path]:
        return self._stub_keypair_dir / "ssh_key", self._stub_keypair_dir / "ssh_key.pub"


def test_build_offline_details_from_lease_preserves_host_and_failure_reason(tmp_path: Path) -> None:
    """When outer SSH is unreachable, the lease-only fallback must keep the host visible.

    Regression test for the branch's stated fix: even in the worst-case
    "no SSH at all" path, ``mngr list`` should still emit a HostDetails
    row with the SSH target populated (so the user can see what we tried
    to reach) and ``failure_reason`` carrying the underlying error.
    """
    provider_name = ProviderInstanceName("imbue-cloud-test")
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    lease = LeasedHostInfo(
        host_db_id=LeaseDbId("lease-db-id"),
        vps_ip="203.0.113.42",
        ssh_port=22,
        ssh_user="user1",
        container_ssh_port=2222,
        agent_id=str(agent_id),
        host_id=str(host_id),
        attributes={},
        leased_at="2025-01-01T00:00:00Z",
    )
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName(str(host_id)),
        provider_name=provider_name,
        host_state=HostState.CRASHED,
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(str(agent_id)),
        provider_name=provider_name,
    )
    failure_message = "outer SSH unreachable: connect to host 203.0.113.42 port 22: Connection timed out"
    provider = _StubImbueCloudProvider.model_construct(
        name=provider_name,
        _stub_keypair_dir=tmp_path,
    )

    host_details, agent_details_list = provider._build_offline_details_from_lease(
        host_ref=host_ref,
        agent_refs=[agent_ref],
        lease=lease,
        failure_message=failure_message,
    )

    # The host is NOT dropped from the listing -- this is the primary contract.
    assert host_details.id == host_id
    # SSH info is populated from the lease so the user can see what we tried
    # to connect to.
    assert host_details.ssh is not None
    assert host_details.ssh.user == lease.ssh_user
    assert host_details.ssh.host == lease.vps_ip
    assert host_details.ssh.port == lease.container_ssh_port
    # State defaults to CRASHED in the lease-only fallback (we have no
    # outer-SSH-derived state to be more specific).
    assert host_details.state == HostState.CRASHED
    # ``failure_reason`` carries the underlying error.
    assert host_details.failure_reason == failure_message
    # One agent_details per agent_ref, all attached to the offline host.
    assert len(agent_details_list) == 1
    assert agent_details_list[0].id == agent_id
    assert agent_details_list[0].host == host_details
