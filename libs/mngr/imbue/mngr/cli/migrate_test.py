"""Unit tests for the migrate CLI command."""

import click
import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.migrate import migrate
from imbue.mngr.main import cli


def test_migrate_command_exists() -> None:
    """The 'migrate' command should be registered on the CLI group."""
    ctx = click.Context(cli)
    assert cli.get_command(ctx, "migrate") is not None


def test_migrate_is_not_clone() -> None:
    """Migrate should be a distinct command object from clone."""
    ctx = click.Context(cli)
    assert cli.get_command(ctx, "migrate") is not cli.get_command(ctx, "clone")


def test_migrate_is_not_create() -> None:
    """Migrate should be a distinct command object from create."""
    ctx = click.Context(cli)
    assert cli.get_command(ctx, "migrate") is not cli.get_command(ctx, "create")


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


def test_migrate_rejects_from_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Migrate should reject --from in remaining args."""
    result = cli_runner.invoke(
        migrate,
        ["source-agent", "--from", "other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--from" in result.output


def test_migrate_nonexistent_source_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that migrate with nonexistent source agent fails."""
    result = cli_runner.invoke(
        migrate,
        ["nonexistent-source-agent-99812"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
