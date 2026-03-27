"""Unit tests for the mngr-mind-chat plugin registration."""

from collections.abc import Sequence

import click

from imbue.mngr_mind_chat.plugin import register_cli_commands


def test_register_cli_commands_returns_chat_command() -> None:
    """Verify that register_cli_commands returns the chat command."""
    result = register_cli_commands()

    assert result is not None
    assert isinstance(result, Sequence)
    assert len(result) == 1
    assert isinstance(result[0], click.Command)
    assert result[0].name == "chat"
