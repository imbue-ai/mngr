"""Shared test helpers for the plugin test modules.

These centralize the "build a fresh plugin manager, install it as the module
singleton, restore the previous one on exit" dance that the individual plugin
test files used to re-implement independently (each copy diverging subtly in
what it restored).
"""

from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

import click
import pluggy

import imbue.mngr.main
from imbue.mngr import hookimpl
from imbue.mngr.main import _register_plugin_commands
from imbue.mngr.main import cli
from imbue.mngr.main import reset_plugin_manager
from imbue.mngr.plugins import hookspecs


@contextmanager
def plugin_manager_installed(plugins: Sequence[Any]) -> Generator[pluggy.PluginManager, None, None]:
    """Create a fresh plugin manager with ``plugins`` registered and install it as the module singleton.

    Resets the module-level plugin manager, builds a new ``pluggy.PluginManager``
    with the mngr hookspecs and the given plugins registered, swaps it into
    ``imbue.mngr.main._plugin_manager_container``, and restores the previous
    manager on exit.
    """
    reset_plugin_manager()
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    for plugin in plugins:
        pm.register(plugin)

    old_pm = imbue.mngr.main._plugin_manager_container["pm"]
    imbue.mngr.main._plugin_manager_container["pm"] = pm
    try:
        yield pm
    finally:
        imbue.mngr.main._plugin_manager_container["pm"] = old_pm


@contextmanager
def plugin_commands_registered(plugins: Sequence[Any]) -> Generator[list[click.Command], None, None]:
    """Install ``plugins`` and run the production command-registration wiring against the real ``cli``.

    This drives the actual ``_register_plugin_commands()`` function (rather than a
    test-local copy of its loop), so a regression in that function -- dropping
    commands, skipping the hook, mishandling the ``command.name is None`` guard --
    would be caught. Yields the list of commands the production wiring added to
    ``cli`` and removes exactly those again on exit, leaving the shared ``cli``
    group unchanged.
    """
    with plugin_manager_installed(plugins):
        commands_before = set(cli.commands)
        added_commands = _register_plugin_commands()
        try:
            yield added_commands
        finally:
            for name in set(cli.commands) - commands_before:
                del cli.commands[name]


class LifecycleTracker:
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
