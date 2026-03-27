from collections.abc import Sequence

import click

from imbue.mngr_file.plugin import register_cli_commands


def test_register_cli_commands_returns_file_group() -> None:
    result = register_cli_commands()

    assert result is not None
    assert isinstance(result, Sequence)
    assert len(result) == 1
    assert isinstance(result[0], click.Group)
    assert result[0].name == "file"


def test_file_group_has_expected_subcommands() -> None:
    result = register_cli_commands()
    assert result is not None
    group = result[0]
    assert isinstance(group, click.Group)

    command_names = set(group.commands.keys())
    assert command_names == {"get", "put", "list"}
