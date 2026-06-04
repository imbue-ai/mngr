from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_latchkey.remote_gateway import LATCHKEY_VERSION
from imbue.mngr_latchkey.remote_gateway import RemoteGatewayError
from imbue.mngr_latchkey.remote_gateway import ensure_latchkey_installed


class _Recorded(MutableModel):
    """One recorded ``execute_idempotent_command`` invocation."""

    command: str = Field(description="The command string passed to the outer host")
    timeout_seconds: float | None = Field(default=None, description="Timeout passed in (if any)")


class _StubOuter(MutableModel):
    """Stub outer host that records commands and returns a canned result.

    Implements only the subset of ``OuterHostInterface`` that
    ``ensure_latchkey_installed`` touches (``execute_idempotent_command`` and
    ``get_name``).
    """

    name: str = Field(default="vps-test", description="Display name returned by get_name")
    result: CommandResult = Field(
        default_factory=lambda: CommandResult(stdout="", stderr="", success=True),
        description="Canned result returned for every command",
    )
    recorded: list[_Recorded] = Field(default_factory=list, description="Each call recorded in order")

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
        self.recorded.append(_Recorded(command=command, timeout_seconds=timeout_seconds))
        return self.result


def _outer(result: CommandResult, name: str = "vps-test") -> OuterHostInterface:
    """Build a stub outer host typed as ``OuterHostInterface``.

    ``cast`` is used because the stub is structurally-but-not-nominally an
    OuterHostInterface (the interface has many other abstract methods that the
    function under test never calls).
    """
    return cast(OuterHostInterface, _StubOuter(name=name, result=result))


def _stub(outer: OuterHostInterface) -> _StubOuter:
    return cast(_StubOuter, outer)


def test_ensure_latchkey_installed_issues_single_idempotent_command() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    assert len(_stub(outer).recorded) == 1


def test_ensure_latchkey_installed_pins_the_version_in_the_npm_install() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    command = _stub(outer).recorded[0].command
    assert f"npm install -g latchkey@{LATCHKEY_VERSION}" in command
    # Reinstall is gated behind a version mismatch check, not unconditional.
    assert f'!= "{LATCHKEY_VERSION}"' in command


def test_ensure_latchkey_installed_gates_each_component_behind_a_presence_check() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    command = _stub(outer).recorded[0].command
    assert "command -v curl" in command
    assert "command -v node" in command
    assert "command -v npm" in command
    assert "deb.nodesource.com/setup_22.x" in command
    assert "apt-get install -y nodejs" in command
    # POSIX sh compatibility: must not rely on bash-only pipefail.
    assert "pipefail" not in command
    assert command.startswith("set -e")


def test_ensure_latchkey_installed_uses_generous_install_timeout() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    assert _stub(outer).recorded[0].timeout_seconds == 300.0


def test_ensure_latchkey_installed_raises_on_failure_with_stderr_in_message() -> None:
    outer = _outer(CommandResult(stdout="", stderr="E: Unable to locate package nodejs", success=False))
    with pytest.raises(RemoteGatewayError, match="Unable to locate package nodejs"):
        ensure_latchkey_installed(outer)


def test_ensure_latchkey_installed_falls_back_to_stdout_when_stderr_empty() -> None:
    outer = _outer(CommandResult(stdout="npm ERR! network timeout", stderr="", success=False))
    with pytest.raises(RemoteGatewayError, match="npm ERR! network timeout"):
        ensure_latchkey_installed(outer)
