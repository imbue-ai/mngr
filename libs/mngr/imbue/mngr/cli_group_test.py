"""Tests for properties of the assembled CLI group."""

import click

from imbue.mngr.main import cli
from imbue.mngr.utils.builtin_command_specs import get_builtin_alias_to_canonical


def test_all_cli_commands_are_single_word() -> None:
    """Ensure all CLI command names are single words (no spaces, hyphens, or underscores).

    This is CRITICAL for the MNGR_COMMANDS_<COMMANDNAME>_<PARAMNAME> env var parsing
    to work correctly. If command names contained underscores, parsing would be ambiguous.

    For example, if a command was named "foo_bar" and a param was "baz", the env var
    would be "MNGR_COMMANDS_FOO_BAR_BAZ", which could be interpreted as either:
        - command="foo", param="bar_baz"
        - command="foo_bar", param="baz"

    By requiring single-word commands, we avoid this ambiguity.

    Any future plugins that register custom commands MUST also follow this convention.

    Note: built-in commands are loaded lazily via ``AliasAwareGroup`` and do not appear in
    ``cli.commands`` until resolved, so we iterate ``list_commands`` / ``get_command`` to
    cover both built-in and plugin-registered commands. Built-in aliases live exclusively
    in the lazy registry (``get_builtin_alias_to_canonical``) and are added explicitly so
    the test enforces the single-word rule on aliases as well.
    """
    assert isinstance(cli, click.Group), "cli should be a click.Group"

    ctx = click.Context(cli)
    names_to_check: set[str] = set(cli.list_commands(ctx))
    # Defensive fallback: ``cli.list_commands`` already returns every key from
    # ``cli.commands`` (click does not filter duplicates), so this merge is a
    # no-op today. Kept in case a future click or AliasAwareGroup change stops
    # surfacing some plugin-registered alias through ``list_commands``.
    names_to_check.update(cli.commands.keys())
    # Built-in aliases (e.g. ``ls`` -> ``list``, ``cfg`` -> ``config``) live in the
    # lazy-load registry inside ``imbue.mngr.main`` and never enter ``cli.commands`` /
    # ``list_commands``, so include them explicitly to keep the single-word rule
    # enforced on aliases too.
    names_to_check.update(get_builtin_alias_to_canonical().keys())
    # Resolve each command so its canonical name (which may differ from the registered
    # name for aliases) is also validated.
    for name in list(names_to_check):
        cmd = cli.get_command(ctx, name)
        if cmd is not None and cmd.name is not None:
            names_to_check.add(cmd.name)

    invalid_commands = sorted(
        command_name
        for command_name in names_to_check
        if " " in command_name or "-" in command_name or "_" in command_name
    )

    assert not invalid_commands, (
        f"CLI command names must be single words (no spaces, hyphens, or underscores) "
        f"for MNGR_COMMANDS_<COMMANDNAME>_<PARAMNAME> env var parsing to work. "
        f"Invalid commands: {invalid_commands}"
    )
