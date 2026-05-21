from imbue.mngr_uncapped_claude.plugin import register_cli_commands


def test_register_cli_commands_returns_uncapped_claude_command() -> None:
    commands = register_cli_commands()
    assert commands is not None
    assert len(commands) == 1
    assert commands[0].name == "uncapped-claude"
