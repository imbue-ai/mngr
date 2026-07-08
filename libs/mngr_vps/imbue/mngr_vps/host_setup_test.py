import base64
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.host_setup import PINNED_DOCKER_VERSION
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE
from imbue.mngr_vps.host_setup import apply_host_setup_on_outer
from imbue.mngr_vps.host_setup import build_host_setup_steps
from imbue.mngr_vps.host_setup import build_remote_script_command


class _StubOuter(MutableModel):
    """Records each idempotent command and returns canned results from a FIFO."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    responses: list[CommandResult] = Field(default_factory=list, description="FIFO of responses; default-success")
    recorded_commands: list[str] = Field(default_factory=list, description="Each command recorded in order")

    def get_name(self) -> str:
        return "stub-outer"

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded_commands.append(command)
        if self.responses:
            return self.responses.pop(0)
        return CommandResult(stdout="", stderr="", success=True)


def _outer(*responses: CommandResult) -> OuterHostInterface:
    return cast(OuterHostInterface, _StubOuter(responses=list(responses)))


def _stub(outer: OuterHostInterface) -> _StubOuter:
    return cast(_StubOuter, outer)


def _decode_remote_command(command: str) -> str:
    """Recover the original script from an ``echo <b64> | base64 -d | sh`` command."""
    encoded = command.split(" | ", 1)[0].removeprefix("echo ")
    return base64.b64decode(encoded).decode("utf-8")


def test_build_host_setup_steps_minimal_order() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    descriptions = [step.description for step in steps]
    # Base packages must come first (Docker apt repo needs curl/ca-certificates/gnupg),
    # then Docker, then sshd tuning. No gVisor or qemu steps when both are disabled.
    assert descriptions[0].startswith("Install base packages")
    assert "Docker" in descriptions[1]
    assert descriptions[-1].startswith("Tune sshd")
    assert not any("gVisor" in d or "runsc" in d for d in descriptions)
    assert not any("qemu" in d for d in descriptions)


def test_build_host_setup_steps_pins_docker_version() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    docker_step = next(step for step in steps if "Docker" in step.description)
    # The apt version is derived per-distro from /etc/os-release at run time (so
    # the same step works on Debian-family Vultr/OVH/AWS outers and on GCP's
    # Ubuntu), so assert the pinned core + the derivation rather than a literal.
    assert PINNED_DOCKER_VERSION in docker_step.script
    assert 'DOCKER_APT_VERSION="' in docker_step.script
    assert "~${ID}.${VERSION_ID}~${VERSION_CODENAME}" in docker_step.script
    assert 'docker-ce="${DOCKER_APT_VERSION}"' in docker_step.script
    assert "--allow-downgrades" in docker_step.script
    assert "get.docker.com" not in docker_step.script


def test_build_host_setup_steps_includes_gvisor_when_requested() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=True, is_qemu_purge_enabled=False)
    gvisor_step = next(step for step in steps if "gVisor" in step.description)
    assert f"gvisor/releases/release/{PINNED_GVISOR_RELEASE}" in gvisor_step.script
    # runsc is registered with --overlay2=none so the container's writable layer
    # persists across a docker restart (the default per-sandbox overlay loses it).
    assert "runsc install -- --overlay2=none" in gvisor_step.script
    # The binary download is skipped when runsc is already present, and the
    # daemon (re)registration is skipped when the flag is already configured.
    assert "command -v runsc" in gvisor_step.script
    assert "--overlay2=none' /etc/docker/daemon.json" in gvisor_step.script


def test_build_host_setup_steps_includes_qemu_purge_when_requested() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=True)
    qemu_step = next(step for step in steps if "qemu" in step.description)
    assert "apt-get purge --auto-remove -y 'qemu*'" in qemu_step.script
    assert "dpkg -l | grep -q qemu" in qemu_step.script


def test_build_host_setup_steps_excludes_ssh_host_key_injection() -> None:
    # SSH host-key injection is first-boot-only and must NOT be re-runnable, or a
    # re-provision would reset the VPS root host key and break known_hosts.
    steps = build_host_setup_steps(install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    for step in steps:
        assert "ssh_deletekeys" not in step.script
        assert "ed25519_private" not in step.script
        assert "ssh_keys" not in step.script


def test_remote_script_command_round_trips() -> None:
    script = "set -e\necho 'hello $(world)'\nprintf '\\n'"
    command = build_remote_script_command(script)
    assert command.endswith("| base64 -d | sh")
    assert _decode_remote_command(command) == script


def test_apply_host_setup_on_outer_runs_all_steps() -> None:
    outer = _outer()
    apply_host_setup_on_outer(outer, install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    expected_steps = build_host_setup_steps(install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    recorded = _stub(outer).recorded_commands
    assert len(recorded) == len(expected_steps)
    # Each recorded command must decode back to the corresponding step's script.
    for command, step in zip(recorded, expected_steps, strict=True):
        assert _decode_remote_command(command) == step.script


def test_apply_host_setup_on_outer_omits_optional_steps_when_disabled() -> None:
    with_optional = _outer()
    apply_host_setup_on_outer(with_optional, install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    without_optional = _outer()
    apply_host_setup_on_outer(without_optional, install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    # Enabling gVisor + qemu purge adds exactly two more commands.
    assert len(_stub(with_optional).recorded_commands) == len(_stub(without_optional).recorded_commands) + 2


def test_apply_host_setup_on_outer_raises_on_step_failure() -> None:
    # First step succeeds, second (Docker) fails -> fatal.
    outer = _outer(
        CommandResult(stdout="", stderr="", success=True),
        CommandResult(stdout="", stderr="E: version not found", success=False),
    )
    with pytest.raises(VpsProvisioningError, match="Docker"):
        apply_host_setup_on_outer(outer, install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    # Stops at the failing step -- does not run sshd tuning afterward.
    assert len(_stub(outer).recorded_commands) == 2
