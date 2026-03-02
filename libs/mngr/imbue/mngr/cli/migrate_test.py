"""Unit tests for the migrate CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.migrate import _user_specified_no_connect
from imbue.mngr.cli.migrate import _user_specified_quiet
from imbue.mngr.cli.migrate import migrate
from imbue.mngr.main import cli


def test_migrate_command_exists() -> None:
    """The 'migrate' command should be registered on the CLI group."""
    assert "migrate" in cli.commands


def test_migrate_is_not_clone() -> None:
    """Migrate should be a distinct command object from clone."""
    assert cli.commands["migrate"] is not cli.commands["clone"]


def test_migrate_is_not_create() -> None:
    """Migrate should be a distinct command object from create."""
    assert cli.commands["migrate"] is not cli.commands["create"]


def test_migrate_requires_source_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Migrate should error when no arguments are provided."""
    result = cli_runner.invoke(
        migrate,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "SOURCE_AGENT" in result.output


def test_migrate_rejects_nonexistent_source_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Migrate should error when the source agent does not exist."""
    result = cli_runner.invoke(
        migrate,
        ["nonexistent-agent-849271"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "not found" in result.output


# --- _user_specified_quiet tests ---


def test_user_specified_quiet_detects_long_flag() -> None:
    assert _user_specified_quiet(["--quiet"], ["mngr", "migrate", "a", "--quiet"]) is True


def test_user_specified_quiet_detects_short_flag() -> None:
    assert _user_specified_quiet(["-q"], ["mngr", "migrate", "a", "-q"]) is True


def test_user_specified_quiet_false_when_absent() -> None:
    assert _user_specified_quiet(["--no-connect"], ["mngr", "migrate", "a", "--no-connect"]) is False


def test_user_specified_quiet_ignores_after_dd() -> None:
    """--quiet after -- should not be detected as a user option."""
    assert (
        _user_specified_quiet(
            ["--quiet"],
            ["mngr", "migrate", "a", "--", "--quiet"],
        )
        is False
    )


# --- _user_specified_no_connect tests ---


def test_user_specified_no_connect_detects_flag() -> None:
    assert (
        _user_specified_no_connect(
            ["--no-connect"],
            ["mngr", "migrate", "a", "--no-connect"],
        )
        is True
    )


def test_user_specified_no_connect_false_when_absent() -> None:
    assert (
        _user_specified_no_connect(
            ["--in", "docker"],
            ["mngr", "migrate", "a", "--in", "docker"],
        )
        is False
    )


def test_user_specified_no_connect_ignores_after_dd() -> None:
    """--no-connect after -- should not be detected as a create option."""
    assert (
        _user_specified_no_connect(
            ["--no-connect"],
            ["mngr", "migrate", "a", "--", "--no-connect"],
        )
        is False
    )
