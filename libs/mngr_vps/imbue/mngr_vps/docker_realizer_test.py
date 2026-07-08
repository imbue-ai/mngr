"""Unit tests for the container realizer's pure (no-outer) surface."""

from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.data_types import PlacementHandle
from imbue.mngr_vps.docker_realizer import CONTAINER_KNOWN_HOSTS_NAME
from imbue.mngr_vps.docker_realizer import CONTAINER_SSH_KEY_NAME
from imbue.mngr_vps.docker_realizer import DockerRealizer
from imbue.mngr_vps.interfaces import SnapshotCapableRealizer


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


def test_docker_realizer_is_snapshot_capable(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """The container realizer can snapshot, so it is a SnapshotCapableRealizer."""
    assert isinstance(_realizer(temp_mngr_ctx, tmp_path), SnapshotCapableRealizer)


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


class _AllFailOuter(MutableModel):
    """Outer host whose every command fails -- so each teardown step raises."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return CommandResult(stdout="", stderr="boom", success=False)


def _container_handle() -> PlacementHandle:
    return PlacementHandle(container_name="mngr-test", volume_name="mngr-host-vol-test")


def test_teardown_placement_raises_cleanup_failed_group_when_steps_fail(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A teardown whose resources exist but can't be removed raises a CleanupFailedGroup.

    With an outer that fails every command, each placement-removal step records a
    HOST_RESOURCE_REMAINS failure, and the realizer raises them as a group (which
    the provider's destroy_host absorbs into its aggregate) rather than swallowing.
    """
    realizer = _realizer(temp_mngr_ctx, tmp_path)
    outer = cast(OuterHostInterface, _AllFailOuter())
    with pytest.raises(CleanupFailedGroup):
        realizer.teardown_placement(outer, HostId.generate(), _container_handle())


class _OneResultOuter(MutableModel):
    """Outer host that returns a single canned ``CommandResult`` for any command."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: CommandResult

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return self.result


def _outer_returning(stdout: str = "", stderr: str = "", success: bool = True) -> OuterHostInterface:
    return cast(
        OuterHostInterface, _OneResultOuter(result=CommandResult(stdout=stdout, stderr=stderr, success=success))
    )


def test_delete_snapshot_placement_succeeds_when_rmi_succeeds(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    realizer = _realizer(temp_mngr_ctx, tmp_path)
    realizer.delete_snapshot_placement(_outer_returning(stdout="deleted"), SnapshotId("sha256:abc"))


def test_delete_snapshot_placement_tolerates_already_gone_image(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """An already-absent image (docker's "No such image") is benign -- the snapshot is gone."""
    outer = _outer_returning(stderr="Error: No such image: sha256:abc", success=False)
    realizer = _realizer(temp_mngr_ctx, tmp_path)
    realizer.delete_snapshot_placement(outer, SnapshotId("sha256:abc"))


def test_delete_snapshot_placement_raises_on_real_rmi_failure(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """A non-"No such image" rmi failure means the image still exists, so it raises rather
    than swallow (unlike the docker provider, which only warns)."""
    outer = _outer_returning(stderr="image is being used by running container abc", success=False)
    realizer = _realizer(temp_mngr_ctx, tmp_path)
    with pytest.raises(MngrError):
        realizer.delete_snapshot_placement(outer, SnapshotId("sha256:abc"))
