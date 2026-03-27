import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.archive import ArchiveCliOptions
from imbue.mngr.cli.archive import archive


def test_archive_cli_options_fields() -> None:
    """Test ArchiveCliOptions has required fields."""
    opts = ArchiveCliOptions(
        agents=("agent1",),
        agent_list=("agent2",),
        archive_all=False,
        force=True,
        dry_run=False,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.agents == ("agent1",)
    assert opts.agent_list == ("agent2",)
    assert opts.archive_all is False
    assert opts.force is True
    assert opts.dry_run is False


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
