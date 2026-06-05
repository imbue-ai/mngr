"""Tests for plugin CLI commands hook.

These drive the production ``_register_plugin_commands()`` wiring (via the shared
``plugin_commands_registered`` helper) against the real ``cli`` group, so a
regression in that function would actually be caught. Each test plugin records
the values it observes into a per-test dict passed to its constructor, avoiding
shared module-level state between tests.
"""

from collections.abc import Sequence
from typing import Any

import click
from click.testing import CliRunner

from imbue.mngr import hookimpl
from imbue.mngr.main import cli
from imbue.mngr.plugins.testing import plugin_commands_registered


class _PluginWithSimpleCommand:
    """A test plugin that adds a simple command."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        captured = self._captured

        @click.command()
        @click.option("--name", default="World", help="Name to greet")
        def greet(name: str) -> None:
            """Greet someone."""
            captured["greet_name"] = name

        return [greet]


class _PluginWithMultipleCommands:
    """A test plugin that adds multiple commands."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        captured = self._captured

        @click.command()
        def cmd_alpha() -> None:
            """Alpha command."""
            captured["alpha_called"] = True

        @click.command()
        def cmd_beta() -> None:
            """Beta command."""
            captured["beta_called"] = True

        return [cmd_alpha, cmd_beta]


class _PluginWithNoCommands:
    """A test plugin that returns None (no commands)."""

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        return None


class _PluginWithEmptyList:
    """A test plugin that returns an empty list."""

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        return []


class _PluginWithContextCommand:
    """A test plugin that adds a command using click context."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        captured = self._captured

        @click.command()
        @click.pass_context
        def ctxcmd(ctx: click.Context) -> None:
            """Command that uses context."""
            captured["ctx_obj_type"] = type(ctx.obj).__name__

        return [ctxcmd]


def test_plugin_registers_simple_command(cli_runner: CliRunner) -> None:
    """A plugin command registered via the production wiring is reachable on the real cli."""
    captured: dict[str, Any] = {}
    with plugin_commands_registered([_PluginWithSimpleCommand(captured)]) as added:
        assert [c.name for c in added] == ["greet"]

        result = cli_runner.invoke(cli, ["greet"])

        assert result.exit_code == 0
        assert captured.get("greet_name") == "World"


def test_plugin_command_with_option(cli_runner: CliRunner) -> None:
    """A plugin command's options work correctly when invoked on the real cli."""
    captured: dict[str, Any] = {}
    with plugin_commands_registered([_PluginWithSimpleCommand(captured)]):
        result = cli_runner.invoke(cli, ["greet", "--name", "Plugin"])

        assert result.exit_code == 0
        assert captured.get("greet_name") == "Plugin"


def test_plugin_registers_multiple_commands(cli_runner: CliRunner) -> None:
    """A plugin can register multiple commands, all reachable on the real cli."""
    captured: dict[str, Any] = {}
    with plugin_commands_registered([_PluginWithMultipleCommands(captured)]) as added:
        assert {c.name for c in added} == {"cmd-alpha", "cmd-beta"}

        result_alpha = cli_runner.invoke(cli, ["cmd-alpha"])
        assert result_alpha.exit_code == 0
        assert captured.get("alpha_called") is True

        result_beta = cli_runner.invoke(cli, ["cmd-beta"])
        assert result_beta.exit_code == 0
        assert captured.get("beta_called") is True


def test_plugin_returning_none_does_not_add_commands(cli_runner: CliRunner) -> None:
    """A plugin returning None registers no commands and leaves the cli command set unchanged."""
    commands_before = set(cli.commands)
    with plugin_commands_registered([_PluginWithNoCommands()]) as added:
        assert added == []
        assert set(cli.commands) == commands_before


def test_plugin_returning_empty_list_does_not_add_commands(cli_runner: CliRunner) -> None:
    """A plugin returning an empty list registers no commands and leaves the cli command set unchanged."""
    commands_before = set(cli.commands)
    with plugin_commands_registered([_PluginWithEmptyList()]) as added:
        assert added == []
        assert set(cli.commands) == commands_before


def test_multiple_plugins_can_register_commands(cli_runner: CliRunner) -> None:
    """Multiple plugins can each register commands, all reachable on the real cli."""
    captured: dict[str, Any] = {}
    plugins = [_PluginWithSimpleCommand(captured), _PluginWithMultipleCommands(captured)]
    with plugin_commands_registered(plugins) as added:
        assert {c.name for c in added} == {"greet", "cmd-alpha", "cmd-beta"}

        result_greet = cli_runner.invoke(cli, ["greet"])
        assert result_greet.exit_code == 0
        assert captured.get("greet_name") == "World"

        result_alpha = cli_runner.invoke(cli, ["cmd-alpha"])
        assert result_alpha.exit_code == 0
        assert captured.get("alpha_called") is True

        result_beta = cli_runner.invoke(cli, ["cmd-beta"])
        assert result_beta.exit_code == 0
        assert captured.get("beta_called") is True


def test_plugin_commands_appear_in_help(cli_runner: CliRunner) -> None:
    """Plugin commands appear in the real cli's help output."""
    captured: dict[str, Any] = {}
    with plugin_commands_registered([_PluginWithSimpleCommand(captured)]):
        result = cli_runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "greet" in result.output


def test_plugin_command_help_shows_description(cli_runner: CliRunner) -> None:
    """Plugin command help shows the command's docstring and options."""
    captured: dict[str, Any] = {}
    with plugin_commands_registered([_PluginWithSimpleCommand(captured)]):
        result = cli_runner.invoke(cli, ["greet", "--help"])

        assert result.exit_code == 0
        assert "Greet someone" in result.output
        assert "--name" in result.output


def test_plugin_command_with_context(cli_runner: CliRunner) -> None:
    """A plugin command can access the click context (which holds the plugin manager)."""
    captured: dict[str, Any] = {}
    with plugin_commands_registered([_PluginWithContextCommand(captured)]):
        result = cli_runner.invoke(cli, ["ctxcmd"])

        assert result.exit_code == 0
        assert captured.get("ctx_obj_type") == "PluginManager"
