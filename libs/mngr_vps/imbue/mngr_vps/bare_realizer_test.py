"""Unit tests for the bare (no-container) realizer's surface, using a recording outer."""

from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps.bare_realizer import BARE_HOST_STORE_DIR
from imbue.mngr_vps.bare_realizer import BareRealizer
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.data_types import PlacementHandle
from imbue.mngr_vps.data_types import RealizePlacementContext
from imbue.mngr_vps.interfaces import SnapshotCapableRealizer
from imbue.mngr_vps.primitives import VPS_KNOWN_HOSTS_NAME
from imbue.mngr_vps.primitives import VPS_SSH_KEY_NAME


class _RecordingOuter(MutableModel):
    """Outer host that records every command and returns a canned result."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    commands: list[str] = Field(default_factory=list, description="Commands issued, in order")
    response_stdout: str = Field(default="", description="stdout returned for every command")
    response_success: bool = Field(default=True, description="success flag returned for every command")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.commands.append(command)
        return CommandResult(stdout=self.response_stdout, stderr="", success=self.response_success)


def _recording_outer() -> tuple[OuterHostInterface, _RecordingOuter]:
    stub = _RecordingOuter()
    return cast(OuterHostInterface, stub), stub


def _bare_realizer(temp_mngr_ctx: MngrContext, key_dir: Path) -> BareRealizer:
    return BareRealizer(
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps")),
        mngr_ctx=temp_mngr_ctx,
        key_dir=key_dir,
        host_dir=temp_mngr_ctx.config.default_host_dir,
        provider_name=ProviderInstanceName("test-vps"),
    )


def _context(
    known_hosts: tuple[str, ...] = (),
    authorized_keys: tuple[str, ...] = (),
) -> RealizePlacementContext:
    return RealizePlacementContext(
        host_id=HostId.generate(),
        name=HostName("test-host"),
        vps_ip="203.0.113.7",
        base_image="debian:bookworm-slim",
        effective_start_args=(),
        docker_build_args=(),
        known_hosts=known_hosts,
        authorized_keys=authorized_keys,
    )


# The bare realizer ignores the placement handle, so the empty handle is enough.
_EMPTY_HANDLE = PlacementHandle()


def test_bare_realizer_is_not_snapshot_capable(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """The bare realizer has no snapshot capability -- it is not a SnapshotCapableRealizer."""
    assert not isinstance(_bare_realizer(temp_mngr_ctx, tmp_path), SnapshotCapableRealizer)


def test_idle_shutdown_powers_off_the_vm_directly(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """The bare agent is the VM root, so idle powers the machine off -- no host-side watcher."""
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    assert realizer.idle_shutdown_command == "shutdown -P now"
    assert realizer.idle_shutdown_stops_host is True


def test_host_dir_path_on_outer_is_under_the_fixed_store_dir(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    assert realizer.host_dir_path_on_outer(HostId.generate()) == BARE_HOST_STORE_DIR / "host_dir"


def test_agent_endpoint_is_vps_port_22_with_vps_key(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """The agent IS the VM root account: the endpoint reuses the VPS keypair on port 22."""
    endpoint = _bare_realizer(temp_mngr_ctx, tmp_path).agent_endpoint("203.0.113.7")
    assert endpoint.hostname == "203.0.113.7"
    assert endpoint.port == 22
    assert endpoint.ssh_user == "root"
    assert endpoint.private_key_path == tmp_path / VPS_SSH_KEY_NAME
    assert endpoint.known_hosts_path == tmp_path / VPS_KNOWN_HOSTS_NAME
    assert endpoint.private_key_path.exists()


def test_realize_placement_installs_packages_and_seeds_store_without_container(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """realize_placement sets up the host_dir symlink + store layout and returns an empty placement."""
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    outer, stub = _recording_outer()

    placement = realizer.realize_placement(outer, _context())

    # No container/volume on a bare placement: the handle is empty.
    assert placement.handle == PlacementHandle()
    assert placement.container_ssh_host_public_key is None

    joined = "\n".join(stub.commands)
    # The agent's mngr host_dir is symlinked at the on-disk store's host_dir.
    assert f"{BARE_HOST_STORE_DIR}/host_dir" in joined
    # The store's agents/ directory is seeded.
    assert f"{BARE_HOST_STORE_DIR}/agents" in joined
    # No docker invocations on the bare path.
    assert "docker" not in joined


def test_realize_placement_applies_extra_known_hosts_and_authorized_keys(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    outer, stub = _recording_outer()
    ctx = _context(
        known_hosts=("example.com ssh-ed25519 AAAAknown",),
        authorized_keys=("ssh-ed25519 AAAAauth",),
    )

    realizer.realize_placement(outer, ctx)

    joined = "\n".join(stub.commands)
    assert "AAAAknown" in joined
    assert "AAAAauth" in joined


def test_start_activity_watcher_runs_on_the_vm(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    outer, stub = _recording_outer()
    realizer.start_activity_watcher(outer, _EMPTY_HANDLE)
    assert any("activity_watcher" in command for command in stub.commands)


def test_lifecycle_steps_are_no_ops(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """stop/start/teardown issue no commands -- machine lifecycle is the substrate's job."""
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    outer, stub = _recording_outer()
    realizer.stop_placement(outer, _EMPTY_HANDLE, timeout_seconds=60.0)
    realizer.start_placement(outer, _EMPTY_HANDLE)
    realizer.teardown_placement(outer, HostId.generate(), _EMPTY_HANDLE)
    assert stub.commands == []


def test_is_placement_running_is_true_when_vm_reachable(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """For bare the agent IS the VM, so a reachable VM is a running host."""
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    outer, _stub = _recording_outer()
    assert realizer.is_placement_running(outer, _EMPTY_HANDLE) is True


def test_read_live_listing_runs_inner_script_directly_on_the_vm(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """The inner listing script runs on the VM (no docker), and the host is reported running."""
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    outer, stub = _recording_outer()

    agent_data, is_running = realizer.read_live_listing(outer, HostId.generate(), "/mngr/hosts/test", "mngr-", "agent")

    assert is_running is True
    # An empty listing output yields no agents.
    assert agent_data == []
    issued = "\n".join(stub.commands)
    # The inner listing script reads the host_dir directly; there is no docker.
    assert "/mngr/hosts/test/data.json" in issued
    assert "docker" not in issued


def test_collect_listing_output_returns_stdout_and_raises_on_failure(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    realizer = _bare_realizer(temp_mngr_ctx, tmp_path)
    ok_outer, ok_stub = _recording_outer()
    ok_stub.response_stdout = "LISTING_OUTPUT"
    assert realizer.collect_listing_output(ok_outer, _EMPTY_HANDLE, "echo hi") == "LISTING_OUTPUT"

    fail_outer, fail_stub = _recording_outer()
    fail_stub.response_success = False
    with pytest.raises(MngrError):
        realizer.collect_listing_output(fail_outer, _EMPTY_HANDLE, "echo hi")
