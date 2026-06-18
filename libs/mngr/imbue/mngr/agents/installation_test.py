from pathlib import Path
from typing import Any

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.utils.testing import make_mngr_ctx

_BINARY = "fakecli"
_INSTALL_CMD = "install-fakecli"


class _RecordingHost(FakeHost):
    """A FakeHost that returns a scripted result per command substring and records calls."""

    result_by_substring: dict[str, CommandResult] = {}
    executed_commands: list[str] = []

    def _execute_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.executed_commands.append(command)
        for substring, result in self.result_by_substring.items():
            if substring in command:
                return result
        return CommandResult(stdout="", stderr="", success=True)


def _host(host_dir: Path, *, is_local: bool, is_present: bool, install_ok: bool = True) -> Any:
    present = CommandResult(stdout="/usr/bin/fakecli" if is_present else "", stderr="", success=is_present)
    return _RecordingHost(
        host_dir=host_dir,
        is_local=is_local,
        result_by_substring={
            "command -v": present,
            _INSTALL_CMD: CommandResult(stdout="", stderr="", success=install_ok),
        },
    )


def _ctx(tmp_path: Path, *, is_auto_approve: bool, is_remote_allowed: bool) -> MngrContext:
    return make_mngr_ctx(
        config=MngrConfig(is_remote_agent_installation_allowed=is_remote_allowed),
        pm=pluggy.PluginManager("mngr"),
        profile_dir=tmp_path / "profile",
        is_interactive=False,
        is_auto_approve=is_auto_approve,
        concurrency_group=ConcurrencyGroup(name="test"),
    )


def test_skips_install_when_already_present(tmp_path: Path) -> None:
    host = _host(tmp_path, is_local=True, is_present=True)
    ensure_cli_installed(host, _ctx(tmp_path, is_auto_approve=False, is_remote_allowed=False), _BINARY, _INSTALL_CMD)
    # Only the presence check ran; no install command was executed.
    assert not any(_INSTALL_CMD in command for command in host.executed_commands)


def test_installs_locally_when_auto_approved(tmp_path: Path) -> None:
    host = _host(tmp_path, is_local=True, is_present=False)
    ensure_cli_installed(host, _ctx(tmp_path, is_auto_approve=True, is_remote_allowed=False), _BINARY, _INSTALL_CMD)
    assert any(_INSTALL_CMD in command for command in host.executed_commands)


def test_raises_locally_when_not_approved_and_non_interactive(tmp_path: Path) -> None:
    host = _host(tmp_path, is_local=True, is_present=False)
    with pytest.raises(AgentInstallationError, match="not installed"):
        ensure_cli_installed(
            host, _ctx(tmp_path, is_auto_approve=False, is_remote_allowed=False), _BINARY, _INSTALL_CMD
        )


def test_installs_remotely_when_allowed(tmp_path: Path) -> None:
    host = _host(tmp_path, is_local=False, is_present=False)
    ensure_cli_installed(host, _ctx(tmp_path, is_auto_approve=False, is_remote_allowed=True), _BINARY, _INSTALL_CMD)
    assert any(_INSTALL_CMD in command for command in host.executed_commands)


def test_raises_remotely_when_disabled(tmp_path: Path) -> None:
    host = _host(tmp_path, is_local=False, is_present=False)
    with pytest.raises(AgentInstallationError, match="remote installation is disabled"):
        ensure_cli_installed(
            host, _ctx(tmp_path, is_auto_approve=False, is_remote_allowed=False), _BINARY, _INSTALL_CMD
        )


def test_raises_when_install_command_fails(tmp_path: Path) -> None:
    host = _host(tmp_path, is_local=False, is_present=False, install_ok=False)
    with pytest.raises(AgentInstallationError, match="Failed to install"):
        ensure_cli_installed(
            host, _ctx(tmp_path, is_auto_approve=False, is_remote_allowed=True), _BINARY, _INSTALL_CMD
        )
