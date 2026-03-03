from typing import Any

import click
import pluggy
import pytest
from click.testing import CliRunner

import imbue.mng.main
from imbue.mng import hookimpl
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.main import cli


class _LifecycleTracker:
    """A test plugin that records lifecycle hook invocations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @hookimpl
    def on_startup(self) -> None:
        self.calls.append(("on_startup", {}))

    @hookimpl
    def on_shutdown(self) -> None:
        self.calls.append(("on_shutdown", {}))

    @hookimpl
    def on_before_command(self, command_name: str, command_params: dict[str, Any]) -> None:
        self.calls.append(("on_before_command", {"command_name": command_name, "command_params": command_params}))

    @hookimpl
    def on_after_command(self, command_name: str, command_params: dict[str, Any]) -> None:
        self.calls.append(("on_after_command", {"command_name": command_name, "command_params": command_params}))

    @hookimpl
    def on_error(self, command_name: str, command_params: dict[str, Any], error: BaseException) -> None:
        self.calls.append(
            ("on_error", {"command_name": command_name, "command_params": command_params, "error": error})
        )

    @property
    def hook_names(self) -> list[str]:
        return [name for name, _ in self.calls]


class _AbortingPlugin:
    """A test plugin that aborts execution by raising in on_before_command."""

    @hookimpl
    def on_before_command(self, command_name: str, command_params: dict[str, Any]) -> None:
        raise click.Abort()


@pytest.fixture()
def lifecycle_tracker(plugin_manager: pluggy.PluginManager) -> _LifecycleTracker:
    """Register a lifecycle tracker plugin and install the plugin manager as the module singleton."""
    tracker = _LifecycleTracker()
    plugin_manager.register(tracker)
    imbue.mng.main._plugin_manager_container["pm"] = plugin_manager
    return tracker


# --- Success path (mng list) ---


def test_hooks_fire_on_successful_command(lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner) -> None:
    """All lifecycle hooks fire in the correct order on a successful command."""
    cli_runner.invoke(cli, ["list"])

    assert lifecycle_tracker.hook_names == [
        "on_startup",
        "on_before_command",
        "on_after_command",
        "on_shutdown",
    ]


def test_on_before_command_receives_correct_command_name(
    lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner
) -> None:
    """on_before_command receives the canonical command name."""
    cli_runner.invoke(cli, ["list"])

    before_calls = [(name, data) for name, data in lifecycle_tracker.calls if name == "on_before_command"]
    assert len(before_calls) == 1
    assert before_calls[0][1]["command_name"] == "list"


def test_on_before_command_receives_correct_params(
    lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner
) -> None:
    """on_before_command receives the resolved command parameters dict."""
    cli_runner.invoke(cli, ["list", "--format", "json"])

    before_calls = [(name, data) for name, data in lifecycle_tracker.calls if name == "on_before_command"]
    assert len(before_calls) == 1
    assert before_calls[0][1]["command_params"]["output_format"] == "json"


def test_on_after_command_receives_correct_command_name(
    lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner
) -> None:
    """on_after_command receives the canonical command name on success."""
    cli_runner.invoke(cli, ["list"])

    after_calls = [(name, data) for name, data in lifecycle_tracker.calls if name == "on_after_command"]
    assert len(after_calls) == 1
    assert after_calls[0][1]["command_name"] == "list"


def test_alias_resolves_to_canonical_command_name(lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner) -> None:
    """Invoking via alias (ls) still reports the canonical command name (list) to hooks."""
    cli_runner.invoke(cli, ["ls"])

    before_calls = [(name, data) for name, data in lifecycle_tracker.calls if name == "on_before_command"]
    assert len(before_calls) == 1
    assert before_calls[0][1]["command_name"] == "list"


# --- Error path (mng destroy nonexistent) ---


def test_on_error_fires_on_command_failure(lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner) -> None:
    """on_error fires (and on_after_command does not) when a command raises."""
    cli_runner.invoke(cli, ["destroy", "nonexistent-agent-xyz"])

    assert lifecycle_tracker.hook_names == [
        "on_startup",
        "on_before_command",
        "on_error",
        "on_shutdown",
    ]


def test_on_error_receives_correct_command_name(lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner) -> None:
    """on_error receives the canonical command name."""
    cli_runner.invoke(cli, ["destroy", "nonexistent-agent-xyz"])

    error_calls = [(name, data) for name, data in lifecycle_tracker.calls if name == "on_error"]
    assert len(error_calls) == 1
    assert error_calls[0][1]["command_name"] == "destroy"


def test_on_error_receives_exception(lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner) -> None:
    """on_error receives the actual exception that was raised."""
    cli_runner.invoke(cli, ["destroy", "nonexistent-agent-xyz"])

    error_calls = [(name, data) for name, data in lifecycle_tracker.calls if name == "on_error"]
    assert len(error_calls) == 1
    assert isinstance(error_calls[0][1]["error"], AgentNotFoundError)


def test_on_after_command_not_called_on_error(lifecycle_tracker: _LifecycleTracker, cli_runner: CliRunner) -> None:
    """on_after_command does NOT fire when a command raises."""
    cli_runner.invoke(cli, ["destroy", "nonexistent-agent-xyz"])

    assert "on_after_command" not in lifecycle_tracker.hook_names


# --- Abort path (plugin raises in on_before_command) ---


def test_plugin_can_abort_via_on_before_command(
    lifecycle_tracker: _LifecycleTracker,
    plugin_manager: pluggy.PluginManager,
    cli_runner: CliRunner,
) -> None:
    """A plugin raising in on_before_command aborts execution; on_after_command never fires."""
    plugin_manager.register(_AbortingPlugin())

    result = cli_runner.invoke(cli, ["list"])

    assert result.exit_code != 0
    assert "on_after_command" not in lifecycle_tracker.hook_names


# --- Multiple plugins ---


def test_multiple_plugin_hooks_all_fire(
    plugin_manager: pluggy.PluginManager,
    cli_runner: CliRunner,
) -> None:
    """When multiple tracker plugins are registered, both record all hooks."""
    tracker1 = _LifecycleTracker()
    tracker2 = _LifecycleTracker()
    plugin_manager.register(tracker1)
    plugin_manager.register(tracker2)
    imbue.mng.main._plugin_manager_container["pm"] = plugin_manager

    cli_runner.invoke(cli, ["list"])

    expected = [
        "on_startup",
        "on_before_command",
        "on_after_command",
        "on_shutdown",
    ]
    assert tracker1.hook_names == expected
    assert tracker2.hook_names == expected


# --- Disabled plugin hooks should not fire ---


def test_blocked_plugin_hooks_do_not_fire(
    plugin_manager: pluggy.PluginManager,
    cli_runner: CliRunner,
) -> None:
    """Hooks from a plugin that has been blocked via pm.set_blocked() should not fire."""
    tracker = _LifecycleTracker()
    plugin_manager.register(tracker, name="test-blocked-plugin")

    # Block the plugin -- this simulates what create_plugin_manager does
    # for config-disabled plugins
    plugin_manager.set_blocked("test-blocked-plugin")

    imbue.mng.main._plugin_manager_container["pm"] = plugin_manager

    cli_runner.invoke(cli, ["list"])

    # The tracker's hooks should NOT have fired since the plugin was blocked
    assert tracker.hook_names == []
