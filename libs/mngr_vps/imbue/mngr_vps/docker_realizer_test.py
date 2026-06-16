"""Unit tests for the container realizer's pure (no-outer) surface."""

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.docker_realizer import CONTAINER_KNOWN_HOSTS_NAME
from imbue.mngr_vps.docker_realizer import CONTAINER_SSH_KEY_NAME
from imbue.mngr_vps.docker_realizer import DockerRealizer


def _realizer(temp_mngr_ctx: MngrContext, key_dir: Path, container_ssh_port: int = 2222) -> DockerRealizer:
    return DockerRealizer(
        config=VpsProviderConfig(
            backend=ProviderBackendName("test-vps-docker"),
            container_ssh_port=container_ssh_port,
        ),
        mngr_ctx=temp_mngr_ctx,
        key_dir=key_dir,
        host_dir=temp_mngr_ctx.config.default_host_dir,
        provider_name=ProviderInstanceName("test-vps-docker"),
    )


def test_supports_snapshots_is_true(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    assert _realizer(temp_mngr_ctx, tmp_path).supports_snapshots is True


def test_idle_shutdown_signals_container_pid1_and_needs_a_host_watcher(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A container can't power off its host, so idle signals PID 1 and a host-side watcher is needed."""
    realizer = _realizer(temp_mngr_ctx, tmp_path)
    assert realizer.idle_shutdown_command == "kill -TERM 1"
    assert realizer.idle_shutdown_stops_host is False


def test_host_dir_path_on_outer_is_under_the_btrfs_subvolume(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    realizer = _realizer(temp_mngr_ctx, tmp_path)
    host_id = HostId.generate()
    expected = realizer.config.btrfs_mount_path / host_id.get_uuid().hex / "host_dir"
    assert realizer.host_dir_path_on_outer(host_id) == expected


def test_agent_endpoint_targets_container_port_with_container_key(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """The agent endpoint is the VPS IP at the container sshd port, with the container keypair."""
    realizer = _realizer(temp_mngr_ctx, tmp_path, container_ssh_port=2244)
    endpoint = realizer.agent_endpoint("203.0.113.5")

    assert endpoint.hostname == "203.0.113.5"
    assert endpoint.port == 2244
    assert endpoint.known_hosts_path == tmp_path / CONTAINER_KNOWN_HOSTS_NAME
    # The container client key is materialized under key_dir on first use.
    assert endpoint.private_key_path == tmp_path / CONTAINER_SSH_KEY_NAME
    assert endpoint.private_key_path.exists()
    # The container realizer connects as the connector default (root), not an explicit user.
    assert endpoint.ssh_user is None
