"""Tests for plugin CLI commands hook."""

from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

import click
import pluggy
from click.testing import CliRunner

import imbue.mng.main
from imbue.mng import hookimpl
from imbue.mng.main import reset_plugin_manager
from imbue.mng.plugins import hookspecs

# Module-level containers to capture values from test commands.
# This avoids using disallowed output methods (see ratchet tests).
_captured_values: dict[str, Any] = {}


class _PluginWithSimpleCommand:
    """A test plugin that adds a simple command."""

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        @click.command()
        @click.option("--name", default="World", help="Name to greet")
        def greet(name: str) -> None:
            """Greet someone."""
            _captured_values["greet_name"] = name

        return [greet]


class _PluginWithMultipleCommands:
    """A test plugin that adds multiple commands."""

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        @click.command()
        def cmd_alpha() -> None:
            """Alpha command."""
            _captured_values["alpha_called"] = True

        @click.command()
        def cmd_beta() -> None:
            """Beta command."""
            _captured_values["beta_called"] = True

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

    @hookimpl
    def register_cli_commands(self) -> Sequence[click.Command] | None:
        @click.command()
        @click.pass_context
        def ctxcmd(ctx: click.Context) -> None:
            """Command that uses context."""
            _captured_values["ctx_obj_type"] = type(ctx.obj).__name__

        return [ctxcmd]


@contextmanager
def _test_cli_with_plugin(
    plugin: Any,
) -> Generator[click.Group, None, None]:
    """Create a test CLI group with a plugin registered, restoring state on exit."""
    with _test_cli_with_plugins([plugin]) as test_cli:
        yield test_cli


@contextmanager
def _test_cli_with_plugins(
    plugins: Sequence[Any],
) -> Generator[click.Group, None, None]:
    """Create a test CLI group with multiple plugins registered, restoring state on exit."""
    reset_plugin_manager()
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    for plugin in plugins:
        pm.register(plugin)

    old_pm = imbue.mng.main._plugin_manager_container["pm"]
    imbue.mng.main._plugin_manager_container["pm"] = pm

    @click.group()
    @click.pass_context
    def test_cli(ctx: click.Context) -> None:
        ctx.obj = pm

    all_command_lists = pm.hook.register_cli_commands()
    for command_list in all_command_lists:
        if command_list is None:
            continue
        for command in command_list:
            if command.name is None:
                continue
            test_cli.add_command(command)

    try:
        yield test_cli
    finally:
        imbue.mng.main._plugin_manager_container["pm"] = old_pm


def test_plugin_registers_simple_command() -> None:
    """Test that a plugin can register a simple command."""
    _captured_values.clear()
    with _test_cli_with_plugin(_PluginWithSimpleCommand()) as test_cli:
        runner = CliRunner()
        result = runner.invoke(test_cli, ["greet"])

        assert result.exit_code == 0
        assert _captured_values.get("greet_name") == "World"


def test_plugin_command_with_option() -> None:
    """Test that a plugin command's options work correctly."""
    _captured_values.clear()
    with _test_cli_with_plugin(_PluginWithSimpleCommand()) as test_cli:
        runner = CliRunner()
        result = runner.invoke(test_cli, ["greet", "--name", "Plugin"])

        assert result.exit_code == 0
        assert _captured_values.get("greet_name") == "Plugin"


def test_plugin_registers_multiple_commands() -> None:
    """Test that a plugin can register multiple commands."""
    _captured_values.clear()
    with _test_cli_with_plugin(_PluginWithMultipleCommands()) as test_cli:
        runner = CliRunner()

        # Test cmd_alpha
        result_alpha = runner.invoke(test_cli, ["cmd-alpha"])
        assert result_alpha.exit_code == 0
        assert _captured_values.get("alpha_called") is True

        # Test cmd_beta
        result_beta = runner.invoke(test_cli, ["cmd-beta"])
        assert result_beta.exit_code == 0
        assert _captured_values.get("beta_called") is True


def test_plugin_returning_none_does_not_add_commands() -> None:
    """Test that a plugin returning None doesn't break anything."""
    with _test_cli_with_plugin(_PluginWithNoCommands()) as test_cli:
        runner = CliRunner()
        result = runner.invoke(test_cli, ["--help"])

        assert result.exit_code == 0
        # The help should work, but no extra commands should be added
        assert "greet" not in result.output


def test_plugin_returning_empty_list_does_not_add_commands() -> None:
    """Test that a plugin returning an empty list doesn't break anything."""
    with _test_cli_with_plugin(_PluginWithEmptyList()) as test_cli:
        runner = CliRunner()
        result = runner.invoke(test_cli, ["--help"])

        assert result.exit_code == 0
        assert "greet" not in result.output


def test_multiple_plugins_can_register_commands() -> None:
    """Test that multiple plugins can each register commands."""
    _captured_values.clear()
    plugins = [_PluginWithSimpleCommand(), _PluginWithMultipleCommands()]
    with _test_cli_with_plugins(plugins) as test_cli:
        runner = CliRunner()

        # Test greet from _PluginWithSimpleCommand
        result_greet = runner.invoke(test_cli, ["greet"])
        assert result_greet.exit_code == 0
        assert _captured_values.get("greet_name") == "World"

        # Test cmd_alpha from _PluginWithMultipleCommands
        result_alpha = runner.invoke(test_cli, ["cmd-alpha"])
        assert result_alpha.exit_code == 0
        assert _captured_values.get("alpha_called") is True

        # Test cmd_beta from _PluginWithMultipleCommands
        result_beta = runner.invoke(test_cli, ["cmd-beta"])
        assert result_beta.exit_code == 0
        assert _captured_values.get("beta_called") is True


def test_plugin_commands_appear_in_help() -> None:
    """Test that plugin commands appear in the CLI help."""
    with _test_cli_with_plugin(_PluginWithSimpleCommand()) as test_cli:
        runner = CliRunner()
        result = runner.invoke(test_cli, ["--help"])

        assert result.exit_code == 0
        assert "greet" in result.output


def test_plugin_command_help_shows_description() -> None:
    """Test that plugin command help shows the command's docstring."""
    with _test_cli_with_plugin(_PluginWithSimpleCommand()) as test_cli:
        runner = CliRunner()
        result = runner.invoke(test_cli, ["greet", "--help"])

        assert result.exit_code == 0
        assert "Greet someone" in result.output
        assert "--name" in result.output


def test_plugin_command_with_context() -> None:
    """Test that a plugin command can access the click context."""
    _captured_values.clear()
    with _test_cli_with_plugin(_PluginWithContextCommand()) as test_cli:
        runner = CliRunner()
        result = runner.invoke(test_cli, ["ctxcmd"])

        assert result.exit_code == 0
        assert _captured_values.get("ctx_obj_type") == "PluginManager"
