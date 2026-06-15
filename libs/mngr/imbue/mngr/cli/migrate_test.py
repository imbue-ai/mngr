"""Unit tests for the migrate CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.migrate import migrate
from imbue.mngr.main import cli
from imbue.mngr.utils.plugin_testing import PLACEHOLDER_AGENT_TYPE


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
    """Migrate of a nonexistent source agent fails at source resolution.

    Migrate delegates to ``create --from <source>``. We pass ``--type`` so the
    agent-type check (create.py:170-179) passes and resolution proceeds to the
    source lookup, where ``_filter_one_agent`` raises ``UserInputError`` naming
    the missing agent (api/find.py:154). ``UserInputError`` is a ClickException,
    so under the runner it renders as an ``Error:`` line and exits with code 1.
    The autouse test env isolates the host dir, so no agent of this name exists.
    """
    agent_name = "nonexistent-source-agent-99812"
    result = cli_runner.invoke(
        migrate,
        [agent_name, "--type", PLACEHOLDER_AGENT_TYPE],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code == 1
    assert "Could not find agent with ID or name" in result.output
    assert agent_name in result.output
