"""Unit tests for the container realizer's pure (no-outer) surface."""

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vps_docker.docker_realizer import CONTAINER_KNOWN_HOSTS_NAME
from imbue.mngr_vps_docker.docker_realizer import CONTAINER_SSH_KEY_NAME
from imbue.mngr_vps_docker.docker_realizer import DockerRealizer


def _realizer(temp_mngr_ctx: MngrContext, key_dir: Path, container_ssh_port: int = 2222) -> DockerRealizer:
    return DockerRealizer(
        config=VpsDockerProviderConfig(
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
