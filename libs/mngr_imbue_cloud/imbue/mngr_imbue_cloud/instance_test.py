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
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.instance import ImbueCloudProvider
from imbue.mngr_imbue_cloud.instance import _build_delegated_vps_config
from imbue.mngr_imbue_cloud.instance import _map_docker_status_to_host_state
from imbue.mngr_imbue_cloud.instance import build_pool_host_wipe_script
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
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


def test_build_delegated_vps_config_forwards_runtime_knobs() -> None:
    """The slow-path rebuild must carry runsc + hardening args onto the vps_docker config."""
    config = ImbueCloudProviderConfig(
        account=ImbueCloudAccount("a@b.com"),
        docker_runtime="runsc",
        install_gvisor_runtime=True,
        default_start_args=("--workdir=/", "--security-opt=no-new-privileges"),
    )
    vps_config = _build_delegated_vps_config(config)
    assert vps_config.backend == "vps_docker"
    assert vps_config.docker_runtime == "runsc"
    assert vps_config.install_gvisor_runtime is True
    assert vps_config.default_start_args == ("--workdir=/", "--security-opt=no-new-privileges")
    # The connection-shape fields are still forwarded from the imbue_cloud config.
    assert vps_config.host_dir == config.host_dir
    assert vps_config.container_ssh_port == config.container_ssh_port


def test_build_delegated_vps_config_defaults_to_no_runtime() -> None:
    """With an unconfigured imbue_cloud config, no runtime is forced (runc)."""
    config = ImbueCloudProviderConfig(account=ImbueCloudAccount("a@b.com"))
    vps_config = _build_delegated_vps_config(config)
    assert vps_config.docker_runtime is None
    assert vps_config.install_gvisor_runtime is False
    assert vps_config.default_start_args == ()


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
# build_pool_host_wipe_script -- the destroy_host data-wipe script generator.
# Pure function; we assert on the exact command shape that destroy_host's
# SSH transport will execute on the leased VPS.
# =============================================================================


_WIPE_HOST_ID = HostId("host-2e6c6d70f14c4f3cbb14da81f68a5dc9")
_WIPE_HOST_HEX = "2e6c6d70f14c4f3cbb14da81f68a5dc9"


def test_build_pool_host_wipe_script_starts_with_set_plus_e() -> None:
    """Every wipe step is best-effort -- the script must NOT abort on first failure."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert script.startswith("set +e\n"), script[:50]


def test_build_pool_host_wipe_script_ends_with_exit_zero() -> None:
    """Caller relies on a clean exit so a single failed wipe doesn't block release."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert script.rstrip().endswith("exit 0"), script[-50:]


def test_build_pool_host_wipe_script_filters_container_by_host_id_label() -> None:
    """Container lookup must filter on the canonical mngr_vps_docker host-id label.

    ``shlex.quote`` is a no-op for the alphanumeric-with-dashes HostId so the
    rendered filter is unquoted; that's still valid shell because the value
    contains no spaces or metacharacters.
    """
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    expected_label = f"label=com.imbue.mngr.host-id={_WIPE_HOST_ID}"
    assert f"docker ps -a --filter {expected_label}" in script


def test_build_pool_host_wipe_script_removes_container_with_volumes() -> None:
    """``docker rm -f -v`` to drop anonymous volumes attached to the workspace container."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert 'docker rm -f -v "$container_id"' in script


def test_build_pool_host_wipe_script_drops_named_host_volume_by_hex() -> None:
    """The per-host bind-options volume name embeds the host hex; filter by it."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    expected_filter = f"name=mngr-host-vol-{_WIPE_HOST_HEX}"
    assert f"docker volume ls -q --filter {expected_filter}" in script
    assert 'docker volume rm -f "$vol"' in script


def test_build_pool_host_wipe_script_deletes_btrfs_subvolume_with_rm_rf_fallback() -> None:
    """btrfs subvolume delete preferred; falls back to rm -rf for non-btrfs hosts."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    subvol_path = f"/mngr-btrfs/{_WIPE_HOST_HEX}"
    assert f"if [ -d {subvol_path} ]; then" in script
    assert f"btrfs subvolume delete {subvol_path}" in script
    assert f"rm -rf {subvol_path}" in script


def test_build_pool_host_wipe_script_runs_docker_system_prune() -> None:
    """Reclaim everything else docker still holds (stopped containers, dangling images, ...)."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert "docker system prune -a -f --volumes" in script


def test_build_pool_host_wipe_script_wipes_root_and_tmp_preserving_authorized_keys() -> None:
    """``/root`` and ``/tmp`` get wiped; ``authorized_keys`` survives so the pool-mgmt key keeps working."""
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert "find /root -mindepth 1 -maxdepth 1 -not -name .ssh -exec rm -rf {} +" in script
    assert "find /root/.ssh -mindepth 1 -not -name authorized_keys -exec rm -rf {} +" in script
    assert "find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} +" in script


def test_build_pool_host_wipe_script_renders_safe_host_id_inline() -> None:
    """``shlex.quote`` is a no-op on the `[a-z0-9-]` HostId shape, so the
    rendered template carries the host id inline (still valid shell since
    the value has no spaces or metacharacters). The point of routing through
    ``shlex.quote`` is to remain safe if the HostId shape ever broadens; the
    test here pins today's expected rendering.
    """
    script = build_pool_host_wipe_script(_WIPE_HOST_ID)
    assert f"label=com.imbue.mngr.host-id={_WIPE_HOST_ID}" in script
    assert f"name=mngr-host-vol-{_WIPE_HOST_HEX}" in script


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
