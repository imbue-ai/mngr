"""Unit tests for the mng_ttyd plugin."""

from pathlib import Path
from typing import Any
from typing import cast

from imbue.mng.interfaces.host import NamedCommand
from imbue.mng_ttyd.plugin import TTYD_COMMAND
from imbue.mng_ttyd.plugin import TTYD_SERVER_NAME
from imbue.mng_ttyd.plugin import TTYD_WINDOW_NAME
from imbue.mng_ttyd.plugin import override_command_options


class _DummyCommandClass:
    pass


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
    """Verify that the plugin preserves any existing additional commands."""
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
    from types import SimpleNamespace

    from imbue.mng_ttyd.plugin import on_after_provisioning

    host_dir = tmp_path / "host"
    host_dir.mkdir()
    agent_id = "test-agent-123"

    written_files: list[tuple[Path, bytes, str]] = []
    executed_cmds: list[str] = []

    class FakeHost:
        def __init__(self) -> None:
            self.host_dir = host_dir

        def execute_command(self, cmd: str, **kwargs: Any) -> SimpleNamespace:
            executed_cmds.append(cmd)
            return SimpleNamespace(returncode=0)

        def write_file(self, path: Path, content: bytes, mode: str = "0644") -> None:
            written_files.append((path, content, mode))

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id=agent_id)), host=cast(Any, FakeHost()), mng_ctx=cast(Any, SimpleNamespace())
    )

    assert len(written_files) == 1
    script_path, content, mode = written_files[0]
    assert script_path == host_dir / "agents" / agent_id / "commands" / "ttyd" / "agent.sh"
    assert mode == "0755"
    assert b"#!/bin/bash" in content
    assert b"tmux attach" in content


def test_on_after_provisioning_creates_ttyd_directory(tmp_path: Path) -> None:
    """Verify that on_after_provisioning creates the commands/ttyd/ directory."""
    from types import SimpleNamespace

    from imbue.mng_ttyd.plugin import on_after_provisioning

    host_dir = tmp_path / "host"
    host_dir.mkdir()

    executed_cmds: list[str] = []

    class FakeHost:
        def __init__(self) -> None:
            self.host_dir = host_dir

        def execute_command(self, cmd: str, **kwargs: Any) -> SimpleNamespace:
            executed_cmds.append(cmd)
            return SimpleNamespace(returncode=0)

        def write_file(self, path: Path, content: bytes, mode: str = "0644") -> None:
            pass

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, FakeHost()), mng_ctx=cast(Any, SimpleNamespace())
    )

    assert any("mkdir -p" in cmd and "commands/ttyd" in cmd for cmd in executed_cmds)
