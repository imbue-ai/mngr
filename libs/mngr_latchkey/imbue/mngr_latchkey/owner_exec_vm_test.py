from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.owner_exec_vm import OWNER_EXEC_VERSION
from imbue.mngr_latchkey.owner_exec_vm import OwnerExecVmError
from imbue.mngr_latchkey.owner_exec_vm import VM_EXEC_PORT
from imbue.mngr_latchkey.owner_exec_vm import provision_owner_exec_vm


class _Recorded(MutableModel):
    command: str = Field(description="Command passed to the outer host")


class _Written(MutableModel):
    path: str = Field(description="Destination path")
    content: bytes = Field(description="Bytes written")
    mode: str | None = Field(default=None, description="chmod mode")
    is_atomic: bool = Field(default=False, description="Atomic write requested")


class _StubOuter(MutableModel):
    """Records commands / writes; returns a canned result for every command."""

    name: str = Field(default="vm-test", description="get_name value")
    result: CommandResult = Field(
        default_factory=lambda: CommandResult(stdout="", stderr="", success=True),
        description="Canned command result",
    )
    is_local: bool = Field(default=False, description="Whether this outer is the local machine")
    recorded: list[_Recorded] = Field(default_factory=list, description="Commands, in order")
    written: list[_Written] = Field(default_factory=list, description="Writes, in order")

    def get_name(self) -> str:
        return self.name

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(_Recorded(command=command))
        return self.result

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        self.written.append(_Written(path=str(path), content=content, mode=mode, is_atomic=is_atomic))


def _outer(result: CommandResult | None = None, is_local: bool = False) -> OuterHostInterface:
    # Default: every command succeeds and the bridge-address probe resolves, so
    # provisioning runs to completion. Tests that exercise a failure pass their
    # own result.
    if result is None:
        result = CommandResult(stdout="172.17.0.1\n", stderr="", success=True)
    stub = _StubOuter(result=result, is_local=is_local)
    return cast(OuterHostInterface, stub)


def _stub(outer: OuterHostInterface) -> _StubOuter:
    return cast(_StubOuter, outer)


def test_provision_installs_pins_version_and_verifies_checksum() -> None:
    outer = _outer()
    provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
    install_command = _stub(outer).recorded[0].command
    assert OWNER_EXEC_VERSION in install_command
    assert "sha256sum -c" in install_command
    assert f"releases/download/{OWNER_EXEC_VERSION}" in install_command


def test_provision_writes_vm_audience_config() -> None:
    outer = _outer()
    provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
    config = next(w for w in _stub(outer).written if w.path.endswith("config.toml"))
    text = config.content.decode("utf-8")
    assert 'role = "vm"' in text
    assert 'audience = "vm:host-0123456789abcdef0123456789abcdef"' in text
    assert f"listen_port = {VM_EXEC_PORT}" in text
    assert "grants_enabled = false" in text
    assert 'authorized_keys_path = "/root/.ssh/authorized_keys"' in text
    assert config.is_atomic is True


def test_provision_binds_to_the_resolved_docker_bridge_address() -> None:
    # When the docker bridge address resolves, the daemon binds there (off a
    # VPS's public interface) rather than 0.0.0.0.
    outer = _outer(result=CommandResult(stdout="172.17.0.1\n", stderr="", success=True))
    provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
    config = next(w for w in _stub(outer).written if w.path.endswith("config.toml"))
    assert 'listen_host = "172.17.0.1"' in config.content.decode("utf-8")


def test_provision_fails_closed_when_bridge_unresolved() -> None:
    # If the docker bridge address cannot be resolved, provisioning must fail
    # rather than bind a wildcard (which on a VPS would be publicly reachable).
    outer = _outer(result=CommandResult(stdout="", stderr="", success=True))
    with pytest.raises(OwnerExecVmError, match="wildcard"):
        provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
    # Nothing was written, so no wildcard-bound config exists.
    assert not any(w.path.endswith("config.toml") for w in _stub(outer).written)


def test_provision_writes_restart_always_systemd_unit_with_memory_cap() -> None:
    outer = _outer()
    provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
    unit = next(w for w in _stub(outer).written if w.path.endswith("owner-exec-vm.service"))
    text = unit.content.decode("utf-8")
    assert "Restart=always" in text
    assert "MemoryMax=" in text
    assert "--config /etc/owner-exec/config.toml" in text


def test_provision_starts_the_daemon() -> None:
    outer = _outer()
    provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
    assert any("systemctl" in r.command and "owner-exec-vm" in r.command for r in _stub(outer).recorded)


def test_provision_is_a_no_op_on_a_local_outer() -> None:
    outer = _outer(is_local=True)
    provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
    assert _stub(outer).recorded == []
    assert _stub(outer).written == []


def test_provision_raises_when_install_fails() -> None:
    outer = _outer(result=CommandResult(stdout="", stderr="no arch build", success=False))
    with pytest.raises(OwnerExecVmError, match="install owner-exec"):
        provision_owner_exec_vm(outer, HostId("host-0123456789abcdef0123456789abcdef"))
