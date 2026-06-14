"""Unit tests for the imbue_cloud provider instance helpers."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.providers.instance import ImbueCloudProvider


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
        vps_address="203.0.113.42",
        ssh_port=22,
        ssh_user="user1",
        container_ssh_port=2222,
        agent_id=str(agent_id),
        host_id=str(host_id),
        host_name=str(host_id),
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
        offline_field_generators={},
    )

    # The host is NOT dropped from the listing -- this is the primary contract.
    assert host_details.id == host_id
    # SSH info is populated from the lease so the user can see what we tried
    # to connect to.
    assert host_details.ssh is not None
    assert host_details.ssh.user == lease.ssh_user
    assert host_details.ssh.host == lease.vps_address
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


# =============================================================================
# _release_lease_on_failure -- the reliability invariant that a failure after a
# successful lease releases the host back to the pool exactly once (so failed
# fast/slow-path builds never leak a paid lease), while a success releases
# nothing and lets the wrapped result/exception flow through untouched.
# =============================================================================


class _RecordingReleaseClient:
    """Stub connector client that records release_host calls (and reports success)."""

    def __init__(self) -> None:
        self.release_calls: list[str] = []

    def release_host(self, access_token: SecretStr, host_db_id: str) -> bool:
        self.release_calls.append(host_db_id)
        return True


class _ReleaseGuardProvider(ImbueCloudProvider):
    """Provider stub that records local-state cleanup instead of touching disk."""

    _cleanup_calls: list[HostId] = []

    def _cleanup_local_host_state(self, host_id: HostId) -> None:
        self._cleanup_calls.append(host_id)


def _make_release_guard_provider() -> tuple[_ReleaseGuardProvider, _RecordingReleaseClient]:
    client = _RecordingReleaseClient()
    provider = _ReleaseGuardProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"),
        client=client,
        _cleanup_calls=[],
    )
    return provider, client


def test_release_lease_on_failure_releases_once_and_propagates() -> None:
    """A failure inside the guard releases the lease exactly once and re-raises the original error."""
    provider, client = _make_release_guard_provider()
    host_id = HostId.generate()
    original_error = RuntimeError("rebuild blew up")

    with pytest.raises(RuntimeError) as exc_info:
        with provider._release_lease_on_failure(SecretStr("tok"), "lease-db-id", host_id, "slow-path rebuild"):
            raise original_error

    # The ORIGINAL exception must propagate untouched (the guard uses a
    # success flag + finally, not except, so it never swallows or wraps it).
    assert exc_info.value is original_error
    # Exactly one release, against the lease's host_db_id.
    assert client.release_calls == ["lease-db-id"]
    # Local host state is cleaned up so a retry starts from a clean slate.
    assert provider._cleanup_calls == [host_id]


def test_release_lease_on_failure_does_not_release_on_success() -> None:
    """A clean exit must NOT release the lease -- the host was successfully adopted/rebuilt."""
    provider, client = _make_release_guard_provider()
    host_id = HostId.generate()

    with provider._release_lease_on_failure(SecretStr("tok"), "lease-db-id", host_id, "fast-path setup"):
        pass

    assert client.release_calls == []
    assert provider._cleanup_calls == []
