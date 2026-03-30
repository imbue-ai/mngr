import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.archive import ArchiveCliOptions
from imbue.mngr.cli.archive import archive


def test_archive_cli_options_fields() -> None:
    """Test ArchiveCliOptions has required fields."""
    opts = ArchiveCliOptions(
        agents=("agent1",),
        agent_list=("agent2",),
        force=True,
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
    assert opts.force is True


def test_archive_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that archive requires at least one agent."""
    result = cli_runner.invoke(
        archive,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output
