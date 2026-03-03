"""Unit tests for the migrate CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mng.cli.migrate import migrate
from imbue.mng.main import cli


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


def test_migrate_rejects_from_agent_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Migrate should reject --from-agent in remaining args."""
    result = cli_runner.invoke(
        migrate,
        ["source-agent", "--from-agent", "other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--from-agent" in result.output
