"""Tests for properties of the assembled CLI group."""

import click

from imbue.mngr.main import PLUGIN_COMMANDS
from imbue.mngr.main import cli


def test_all_builtin_cli_commands_are_single_word() -> None:
    """Ensure all built-in CLI command names are single words (no spaces, hyphens, or underscores).

    This is CRITICAL for the MNGR_COMMANDS_<COMMANDNAME>_<PARAMNAME> env var parsing
    to work correctly. If command names contained underscores, parsing would be ambiguous.

    For example, if a command was named "foo_bar" and a param was "baz", the env var
    would be "MNGR_COMMANDS_FOO_BAR_BAZ", which could be interpreted as either:
        - command="foo", param="bar_baz"
        - command="foo_bar", param="baz"

    By requiring single-word commands for built-ins, we avoid this ambiguity.

    Plugin-registered commands (e.g. ``mngr imbue_cloud``) are exempted because
    plugin authors choose names that are unique within their plugin namespace
    and the env-var override flow is documented as not supporting them.
    """
    assert isinstance(cli, click.Group), "cli should be a click.Group"

    ctx = click.Context(cli)
    plugin_command_names = {cmd.name for cmd in PLUGIN_COMMANDS if cmd.name is not None}
    names_to_check: set[str] = set(cli.list_commands(ctx))
    # Resolve each command so its canonical name (which may differ from the registered
    # name for aliases) is also validated.
    for name in list(names_to_check):
        cmd = cli.get_command(ctx, name)
        if cmd is not None and cmd.name is not None:
            names_to_check.add(cmd.name)

    invalid_commands = sorted(
        command_name
        for command_name in names_to_check
        if command_name not in plugin_command_names
        and (" " in command_name or "-" in command_name or "_" in command_name)
    )

    assert not invalid_commands, (
        f"Built-in CLI command names must be single words (no spaces, hyphens, or underscores) "
        f"for MNGR_COMMANDS_<COMMANDNAME>_<PARAMNAME> env var parsing to work. "
        f"Invalid commands: {invalid_commands}"
    )
