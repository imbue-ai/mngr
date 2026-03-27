import click

from imbue.imbue_common.pure import pure


@pure
def detect_alias_to_canonical(cli_group: click.Group) -> dict[str, str]:
    """Detect command aliases by comparing registered names to canonical cmd.name.

    When a command is registered under a name different from its cmd.name
    (e.g. via ``cli.add_command(create, name="c")``), the registered name is
    an alias. Returns a dict mapping alias -> canonical name.
    """
    alias_to_canonical: dict[str, str] = {}
    for registered_name, cmd in cli_group.commands.items():
        if cmd.name is not None and registered_name != cmd.name:
            alias_to_canonical[registered_name] = cmd.name
    return alias_to_canonical


@pure
def detect_aliases_by_command(cli_group: click.Group) -> dict[str, list[str]]:
    """Detect command aliases, grouped by canonical name.

    Returns a dict mapping canonical command name -> list of aliases.
    For example: {"create": ["c"], "list": ["ls"], ...}.
    """
    aliases_by_command: dict[str, list[str]] = {}
    for registered_name, cmd in cli_group.commands.items():
        if cmd.name is not None and registered_name != cmd.name:
            aliases_by_command.setdefault(cmd.name, []).append(registered_name)
    return aliases_by_command
