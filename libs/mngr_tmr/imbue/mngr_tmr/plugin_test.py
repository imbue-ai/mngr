"""Unit tests for plugin registration."""

import pluggy

from imbue.mngr_tmr.plugin import register_cli_commands


def test_register_cli_commands_returns_both_tmr_family_commands() -> None:
    commands = register_cli_commands()
    assert commands is not None
    assert [command.name for command in commands] == ["tmr", "tmr-specs"]


def test_plugin_registers_with_pluggy(plugin_manager: pluggy.PluginManager) -> None:
    results = plugin_manager.hook.register_cli_commands()
    command_names = []
    for result in results:
        if result is not None:
            for cmd in result:
                command_names.append(cmd.name)
    assert "tmr" in command_names
    assert "tmr-specs" in command_names
