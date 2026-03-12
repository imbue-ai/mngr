"""Unit tests for the mng_ttyd plugin."""

from typing import Any

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
