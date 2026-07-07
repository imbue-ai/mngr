from click.testing import CliRunner

from imbue.minds.cli_entry import cli


def test_cli_shows_help_lists_top_level_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    # Assert the actual top-level contract -- the three wired-up subcommands --
    # rather than an incidental word from one subcommand's help text. This fails
    # if a subcommand is dropped from the group, and does not break on cosmetic
    # rewords of any subcommand's one-line help.
    for command_name in ("run", "pool", "env"):
        assert command_name in result.output
    assert "run and manage your own persistent" in result.output
