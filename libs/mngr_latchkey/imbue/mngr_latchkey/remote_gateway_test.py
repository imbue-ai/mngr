from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.remote_gateway import INNER_PORT
from imbue.mngr_latchkey.remote_gateway import LATCHKEY_VERSION
from imbue.mngr_latchkey.remote_gateway import OUTER_PORT
from imbue.mngr_latchkey.remote_gateway import RemoteGatewayError
from imbue.mngr_latchkey.remote_gateway import ensure_latchkey_gateway_running
from imbue.mngr_latchkey.remote_gateway import ensure_latchkey_installed
from imbue.mngr_latchkey.remote_gateway import sync_credentials
from imbue.mngr_latchkey.remote_gateway import sync_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir


class _Recorded(MutableModel):
    """One recorded ``execute_idempotent_command`` invocation."""

    command: str = Field(description="The command string passed to the outer host")
    timeout_seconds: float | None = Field(default=None, description="Timeout passed in (if any)")


class _WrittenFile(MutableModel):
    """One recorded ``write_file`` / ``write_text_file`` invocation."""

    path: str = Field(description="Destination path on the VPS")
    content: bytes = Field(description="Bytes written")
    mode: str | None = Field(default=None, description="chmod mode requested (if any)")


class _StubOuter(MutableModel):
    """Stub outer host that records commands / writes and returns a canned result.

    Implements only the subset of ``OuterHostInterface`` that the functions
    under test touch (``execute_idempotent_command``, ``write_file``,
    ``write_text_file``, ``get_name``).
    """

    name: str = Field(default="vps-test", description="Display name returned by get_name")
    result: CommandResult = Field(
        default_factory=lambda: CommandResult(stdout="", stderr="", success=True),
        description="Canned result returned for every command",
    )
    home: str = Field(default="/root", description="Value returned for the $HOME resolution command")
    recorded: list[_Recorded] = Field(default_factory=list, description="Each command recorded in order")
    written: list[_WrittenFile] = Field(default_factory=list, description="Each file write recorded in order")

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
        # Only the dedicated $HOME-resolution probe gets the home response; any
        # other command (install/gateway scripts) returns the configured result.
        if command.strip() == 'echo "$HOME"':
            return CommandResult(stdout=f"{self.home}\n", stderr="", success=True)
        return self.result

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        self.written.append(_WrittenFile(path=str(path), content=content, mode=mode))

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        self.written.append(_WrittenFile(path=str(path), content=content.encode(encoding), mode=mode))


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
    # Version-agnostic: the NodeSource setup URL is present (the major version
    # is a tunable constant, so don't pin it here).
    assert "deb.nodesource.com/setup_" in command
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


def test_sync_credentials_copies_local_file_to_remote_latchkey_dir(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    (latchkey_directory / "credentials.json.enc").write_bytes(b"encrypted-secret-bytes")
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_credentials(outer, latchkey_directory)

    written = _stub(outer).written
    assert len(written) == 1
    assert written[0].path == "/root/.latchkey/credentials.json.enc"
    assert written[0].content == b"encrypted-secret-bytes"
    assert written[0].mode == "0600"


def test_sync_credentials_raises_when_local_file_missing(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    with pytest.raises(RemoteGatewayError, match="credentials file does not exist"):
        sync_credentials(outer, latchkey_directory)


def test_sync_permissions_copies_per_host_file_to_remote_permissions_json(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    local_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    local_path.parent.mkdir(parents=True)
    local_path.write_text('{"rules": [{"slack-api": ["slack-read-all"]}]}')
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_permissions(outer, latchkey_directory, host_id)

    written = _stub(outer).written
    assert len(written) == 1
    assert written[0].path == "/root/.latchkey/permissions.json"
    assert b"slack-read-all" in written[0].content
    assert written[0].mode == "0600"


def test_sync_permissions_falls_back_to_restrictive_default_when_local_missing(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_permissions(outer, latchkey_directory, host_id)

    written = _stub(outer).written
    assert len(written) == 1
    assert written[0].path == "/root/.latchkey/permissions.json"
    # The deny-all default carries an empty rules list and no schemas block.
    assert written[0].content == b'{\n  "rules": []\n}'


def test_sync_permissions_resolves_remote_home_for_the_destination(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    outer = cast(OuterHostInterface, _StubOuter(home="/home/agent"))

    sync_permissions(outer, latchkey_directory, host_id)

    assert _stub(outer).written[0].path == "/home/agent/.latchkey/permissions.json"


def test_sync_permissions_raises_when_home_resolution_fails(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    outer = cast(OuterHostInterface, _StubOuter(home=""))

    with pytest.raises(RemoteGatewayError, match="resolve \\$HOME"):
        sync_permissions(outer, latchkey_directory, host_id)


def test_ports_are_integers() -> None:
    assert isinstance(INNER_PORT, int)
    assert isinstance(OUTER_PORT, int)


def test_ensure_latchkey_gateway_running_starts_detached_gateway_on_inner_port() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_gateway_running(outer)
    assert len(_stub(outer).recorded) == 1
    command = _stub(outer).recorded[0].command
    assert f"LATCHKEY_GATEWAY_PORT={INNER_PORT} nohup latchkey gateway" in command
    # Skips the launch when a gateway is already running.
    assert "pgrep -f 'latchkey gateway'" in command
    # Detached so it outlives the SSH session.
    assert "nohup" in command
    assert "</dev/null" in command


def test_ensure_latchkey_gateway_running_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="latchkey: command not found", success=False))
    with pytest.raises(RemoteGatewayError, match="command not found"):
        ensure_latchkey_gateway_running(outer)
