"""Tests for properties of the assembled CLI group."""

import click

from imbue.mngr.main import PLUGIN_COMMANDS
from imbue.mngr.main import cli


def test_all_builtin_cli_commands_are_single_word() -> None:
    """Ensure all built-in CLI command names are single words (no spaces, hyphens, or underscores).

    Mngr's built-in CLI uses single-word verbs (``create``, ``list``, ``connect``,
    ...). Keeping that convention makes the surface consistent and easy to type;
    new built-in commands that diverge should be flagged here so the choice is
    deliberate.

    Plugin-registered commands (e.g. ``mngr imbue_cloud``) are exempted because
    plugin authors choose names that are unique within their plugin namespace
    and may need multi-word identifiers for clarity.
    """
    assert isinstance(cli, click.Group), "cli should be a click.Group"

    plugin_command_names = {cmd.name for cmd in PLUGIN_COMMANDS if cmd.name is not None}
    invalid_commands = []
    for command_name in cli.commands.keys():
        if command_name in plugin_command_names:
            continue
        if " " in command_name or "-" in command_name or "_" in command_name:
            invalid_commands.append(command_name)

    assert not invalid_commands, (
        f"Built-in CLI command names must be single words (no spaces, hyphens, or underscores) "
        f"to match mngr's CLI naming convention. Invalid commands: {invalid_commands}"
    )
