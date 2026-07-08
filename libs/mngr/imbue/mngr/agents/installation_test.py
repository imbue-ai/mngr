from pathlib import Path
from typing import Any

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.agents.installation import is_pinned_version_present
from imbue.mngr.agents.installation import verify_pinned_cli_version
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


@pytest.mark.parametrize(
    ("version_output", "pinned", "expected"),
    [
        ("pi 1.2.3", "1.2.3", True),
        ("2.1.50 (Claude Code)", "2.1.50", True),
        ("opencode 0.4.10 (linux)", "0.4.10", True),
        ("codex-cli 0.139.0", "0.139.0", True),
        # A leading "v" on either side is ignored.
        ("agy v2.1.50", "2.1.50", True),
        ("pkg 1.2.3", "v1.2.3", True),
        # Pre-release / non-three-part pins are matched verbatim (the win over semver parsing).
        ("pi 1.2.3-rc1", "1.2.3-rc1", True),
        # Token equality, not substring: 1.2.3 must not match 1.2.30.
        ("pkg 1.2.30", "1.2.3", False),
        ("pkg 1.2.4", "1.2.3", False),
        ("no version here", "1.2.3", False),
        ("", "1.2.3", False),
    ],
)
def test_is_pinned_version_present(version_output: str, pinned: str, expected: bool) -> None:
    assert is_pinned_version_present(version_output, pinned) is expected


def _version_probe_host(tmp_path: Path, *, stdout: str = "", stderr: str = "", success: bool = True) -> Any:
    return _RecordingHost(
        host_dir=tmp_path,
        is_local=True,
        result_by_substring={"--version": CommandResult(stdout=stdout, stderr=stderr, success=success)},
    )


def test_verify_pinned_cli_version_passes_on_match(tmp_path: Path) -> None:
    host = _version_probe_host(tmp_path, stdout="fakecli 1.2.3")
    verify_pinned_cli_version(host, command=_BINARY, binary_name=_BINARY, pinned_version="1.2.3")


def test_verify_pinned_cli_version_reads_version_from_stderr(tmp_path: Path) -> None:
    # Some CLIs (e.g. pi) print --version to stderr; both streams must be inspected.
    host = _version_probe_host(tmp_path, stdout="", stderr="0.74.2")
    verify_pinned_cli_version(host, command=_BINARY, binary_name=_BINARY, pinned_version="0.74.2")
    with pytest.raises(AgentInstallationError, match="version mismatch"):
        verify_pinned_cli_version(host, command=_BINARY, binary_name=_BINARY, pinned_version="0.74.3")


def test_verify_pinned_cli_version_passes_on_prerelease_pin(tmp_path: Path) -> None:
    # A pre-release pin verifies verbatim -- the case that a semver-extracting check got wrong.
    host = _version_probe_host(tmp_path, stdout="fakecli 1.2.3-rc1")
    verify_pinned_cli_version(host, command=_BINARY, binary_name=_BINARY, pinned_version="1.2.3-rc1")


def test_verify_pinned_cli_version_raises_on_mismatch(tmp_path: Path) -> None:
    host = _version_probe_host(tmp_path, stdout="fakecli 1.2.4")
    with pytest.raises(AgentInstallationError, match="version mismatch"):
        verify_pinned_cli_version(host, command=_BINARY, binary_name=_BINARY, pinned_version="1.2.3")


def test_verify_pinned_cli_version_raises_when_pin_absent_from_nonempty_output(tmp_path: Path) -> None:
    # Non-empty banner that lacks the pin is a genuine mismatch signal; the error shows the output.
    host = _version_probe_host(tmp_path, stdout="some unexpected banner")
    with pytest.raises(AgentInstallationError, match="some unexpected banner"):
        verify_pinned_cli_version(host, command=_BINARY, binary_name=_BINARY, pinned_version="1.2.3")


def test_verify_pinned_cli_version_skips_when_probe_fails(tmp_path: Path) -> None:
    host = _version_probe_host(tmp_path, stdout="", success=False)
    verify_pinned_cli_version(host, command=_BINARY, binary_name=_BINARY, pinned_version="1.2.3")
