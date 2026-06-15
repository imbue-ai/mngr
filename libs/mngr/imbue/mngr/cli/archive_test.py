import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.archive import archive


def test_archive_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that archive requires at least one agent or --all."""
    result = cli_runner.invoke(
        archive,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent or use --all" in result.output


def test_archive_cannot_combine_agents_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --all cannot be combined with agent names."""
    result = cli_runner.invoke(
        archive,
        ["my-agent", "--all"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify both agent names and --all" in result.output


def test_archive_all_with_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test archiving all agents when none exist."""
    result = cli_runner.invoke(
        archive,
        ["--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No agents found to archive" in result.output


def test_archive_dry_run_all_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--dry-run --all with no agents returns 0."""
    result = cli_runner.invoke(
        archive,
        ["--all", "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No agents found to archive" in result.output
