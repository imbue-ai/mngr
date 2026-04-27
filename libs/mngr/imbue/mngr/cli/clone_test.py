"""Unit tests for the clone CLI command."""

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.clone import _reject_source_agent_options
from imbue.mngr.cli.clone import clone
from imbue.mngr.main import cli


def test_clone_command_exists() -> None:
    """The 'clone' command should be registered on the CLI group."""
    ctx = click.Context(cli)
    assert cli.get_command(ctx, "clone") is not None


def test_clone_is_not_create() -> None:
    """Clone should be a distinct command object from create."""
    ctx = click.Context(cli)
    assert cli.get_command(ctx, "clone") is not cli.get_command(ctx, "create")


def test_clone_requires_source_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Clone should error when no arguments are provided."""
    result = cli_runner.invoke(
        clone,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "SOURCE_AGENT" in result.output


def test_clone_rejects_from_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Clone should reject --from in remaining args."""
    result = cli_runner.invoke(
        clone,
        ["source-agent", "--from", "other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--from" in result.output


def test_clone_rejects_source_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Clone should reject --source in remaining args."""
    result = cli_runner.invoke(
        clone,
        ["source-agent", "--source", "other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--source" in result.output


def test_clone_rejects_from_equals_form(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Clone should reject --from=value form in remaining args."""
    result = cli_runner.invoke(
        clone,
        ["source-agent", "--from=other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--from" in result.output


# --- _reject_source_agent_options with -- boundary tests ---


def test_reject_source_agent_options_allows_from_after_dd() -> None:
    """--from after -- should not be rejected."""
    ctx = click.Context(clone, info_name="clone")
    # before_dd=0 makes the function check args[:0] (empty), so --from is
    # treated as past the separator and must not raise.
    _reject_source_agent_options(["--from", "x"], ctx, before_dd=0)


def test_reject_source_agent_options_rejects_from_before_dd() -> None:
    """--from before -- should still be rejected."""
    ctx = click.Context(clone, info_name="clone")
    with pytest.raises(click.UsageError, match="--from"):
        _reject_source_agent_options(["--from", "x", "--model", "opus"], ctx, before_dd=2)
