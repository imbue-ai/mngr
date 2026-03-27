"""Unit tests for the mng_ttyd plugin."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

from imbue.mng.interfaces.host import NamedCommand
from imbue.mng_ttyd.plugin import TTYD_COMMAND
from imbue.mng_ttyd.plugin import TTYD_INSTALL_COMMAND
from imbue.mng_ttyd.plugin import TTYD_SERVER_NAME
from imbue.mng_ttyd.plugin import TTYD_VERSION
from imbue.mng_ttyd.plugin import TTYD_WINDOW_NAME
from imbue.mng_ttyd.plugin import on_after_provisioning
from imbue.mng_ttyd.plugin import override_command_options


class _DummyCommandClass:
    pass


class _FakeTtydHost:
    """Fake host for testing on_after_provisioning.

    Tracks executed commands and written files. By default, all commands succeed.
    Set ttyd_installed=False to simulate ttyd not being installed on the host.
    """

    def __init__(self, host_dir: Path, *, ttyd_installed: bool = True) -> None:
        self.host_dir = host_dir
        self._ttyd_installed = ttyd_installed
        self.executed_cmds: list[str] = []
        self.written_files: list[tuple[Path, bytes, str]] = []

    def execute_command(self, cmd: str, **kwargs: Any) -> SimpleNamespace:
        self.executed_cmds.append(cmd)
        if "command -v ttyd" in cmd and not self._ttyd_installed:
            return SimpleNamespace(returncode=1, success=False, stdout="", stderr="")
        return SimpleNamespace(returncode=0, success=True, stdout="", stderr="")

    def write_file(self, path: Path, content: bytes, mode: str = "0644") -> None:
        self.written_files.append((path, content, mode))


def test_adds_ttyd_command_to_create() -> None:
    """Verify that the plugin adds a ttyd command when creating agents."""
    params: dict[str, Any] = {"extra_window": ()}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert len(params["extra_window"]) == 1
    assert TTYD_WINDOW_NAME in params["extra_window"][0]
    assert TTYD_COMMAND in params["extra_window"][0]


def test_preserves_existing_extra_windows() -> None:
    """Verify that the plugin preserves any existing extra windows."""
    params: dict[str, Any] = {"extra_window": ('monitor="htop"',)}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert len(params["extra_window"]) == 2
    assert params["extra_window"][0] == 'monitor="htop"'
    assert TTYD_COMMAND in params["extra_window"][1]


def test_does_not_modify_non_create_commands() -> None:
    """Verify that the plugin does not modify params for non-create commands."""
    params: dict[str, Any] = {"extra_window": ()}

    override_command_options(
        command_name="connect",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert params["extra_window"] == ()


def test_handles_missing_extra_window_param() -> None:
    """Verify that the plugin handles the case where extra_window is not yet in params."""
    params: dict[str, Any] = {}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert len(params["extra_window"]) == 1
    assert TTYD_COMMAND in params["extra_window"][0]


def test_ttyd_command_is_parseable_as_named_command() -> None:
    """Verify that the injected command string can be parsed by NamedCommand.from_string."""
    params: dict[str, Any] = {}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    named_cmd = NamedCommand.from_string(params["extra_window"][0])
    assert named_cmd.window_name == TTYD_WINDOW_NAME
    assert str(named_cmd.command) == TTYD_COMMAND


def test_ttyd_command_uses_random_port() -> None:
    """Verify that the ttyd command binds to a random port via -p 0."""
    assert "ttyd -p 0" in TTYD_COMMAND


def test_ttyd_command_enables_url_arg_dispatch() -> None:
    """Verify that the ttyd command uses -a for URL-arg dispatch."""
    assert "ttyd -p 0 -a" in TTYD_COMMAND


def test_ttyd_command_writes_server_log() -> None:
    """Verify that the ttyd command writes to servers/events.jsonl for forwarding server discovery."""
    assert "servers/events.jsonl" in TTYD_COMMAND
    assert TTYD_SERVER_NAME in TTYD_COMMAND
    assert "MNG_AGENT_STATE_DIR" in TTYD_COMMAND
    assert "server_registered" in TTYD_COMMAND
    assert "timestamp" in TTYD_COMMAND
    assert "event_id" in TTYD_COMMAND


def test_ttyd_command_watches_stderr_for_port() -> None:
    """Verify that the command parses the port from ttyd's output."""
    assert "Listening on port:" in TTYD_COMMAND


def test_ttyd_command_skips_log_when_no_state_dir() -> None:
    """Verify that the command gracefully handles MNG_AGENT_STATE_DIR being unset."""
    assert 'if [ -n "$MNG_AGENT_STATE_DIR" ]' in TTYD_COMMAND


def test_ttyd_command_dispatches_to_ttyd_scripts() -> None:
    """Verify that the dispatch script routes to commands/ttyd/<KEY>.sh."""
    assert "commands/ttyd/$KEY.sh" in TTYD_COMMAND


def test_ttyd_command_scans_ttyd_scripts_for_events() -> None:
    """Verify that the port wrapper scans commands/ttyd/*.sh and writes events for each."""
    assert 'commands/ttyd/"*.sh' in TTYD_COMMAND
    assert "basename" in TTYD_COMMAND
    assert "?arg=$_K" in TTYD_COMMAND


# -- on_after_provisioning tests --


def test_on_after_provisioning_writes_agent_script(tmp_path: Path) -> None:
    """Verify that on_after_provisioning writes ttyd/agent.sh to the agent state dir."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    agent_id = "test-agent-123"

    host = _FakeTtydHost(host_dir)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id=agent_id)), host=cast(Any, host), mng_ctx=cast(Any, SimpleNamespace())
    )

    assert len(host.written_files) == 1
    script_path, content, mode = host.written_files[0]
    assert script_path == host_dir / "agents" / agent_id / "commands" / "ttyd" / "agent.sh"
    assert mode == "0755"
    assert b"#!/bin/bash" in content
    assert b"tmux attach" in content


def test_on_after_provisioning_creates_ttyd_directory(tmp_path: Path) -> None:
    """Verify that on_after_provisioning creates the commands/ttyd/ directory."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = _FakeTtydHost(host_dir)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mng_ctx=cast(Any, SimpleNamespace())
    )

    assert any("mkdir -p" in cmd and "commands/ttyd" in cmd for cmd in host.executed_cmds)


def test_on_after_provisioning_installs_ttyd_when_missing(tmp_path: Path) -> None:
    """Verify that on_after_provisioning downloads ttyd binary when it is not already present."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = _FakeTtydHost(host_dir, ttyd_installed=False)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mng_ctx=cast(Any, SimpleNamespace())
    )

    assert any(cmd == TTYD_INSTALL_COMMAND for cmd in host.executed_cmds)


def test_on_after_provisioning_skips_install_when_ttyd_present(tmp_path: Path) -> None:
    """Verify that on_after_provisioning skips ttyd install when it is already present."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = _FakeTtydHost(host_dir, ttyd_installed=True)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mng_ctx=cast(Any, SimpleNamespace())
    )

    assert not any(cmd == TTYD_INSTALL_COMMAND for cmd in host.executed_cmds)


def test_ttyd_install_command_downloads_from_github() -> None:
    """Verify that the install command downloads the correct ttyd version from GitHub releases."""
    assert "github.com/tsl0922/ttyd/releases/download" in TTYD_INSTALL_COMMAND
    assert TTYD_VERSION in TTYD_INSTALL_COMMAND
    assert "/usr/local/bin/ttyd" in TTYD_INSTALL_COMMAND
    assert "uname -m" in TTYD_INSTALL_COMMAND
    assert "chmod +x" in TTYD_INSTALL_COMMAND
