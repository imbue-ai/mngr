"""Unit tests for the clone CLI command."""

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.clone import _args_before_dd_count
from imbue.mngr.cli.clone import _build_create_args
from imbue.mngr.cli.clone import _reject_source_agent_options
from imbue.mngr.cli.clone import clone
from imbue.mngr.main import cli


def test_clone_command_exists() -> None:
    """The 'clone' command should be registered on the CLI group."""
    assert "clone" in cli.commands


def test_clone_is_not_create() -> None:
    """Clone should be a distinct command object from create."""
    assert cli.commands["clone"] is not cli.commands["create"]


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


# --- _build_create_args tests ---


def test_build_create_args_without_double_dash() -> None:
    """Without -- in argv, remaining args are passed through directly."""
    result = _build_create_args(
        source_agent="my-agent",
        remaining=["--provider", "docker"],
        original_argv=["mngr", "clone", "my-agent", "--provider", "docker"],
    )
    assert result == ["--from", "my-agent", "--provider", "docker"]


def test_build_create_args_with_double_dash() -> None:
    """With -- in argv, the separator is re-inserted in create_args."""
    result = _build_create_args(
        source_agent="my-agent",
        remaining=["--model", "opus"],
        original_argv=["mngr", "clone", "my-agent", "--", "--model", "opus"],
    )
    assert result == ["--from", "my-agent", "--", "--model", "opus"]


def test_build_create_args_with_create_options_and_double_dash() -> None:
    """Create options before -- and agent args after -- are split correctly."""
    result = _build_create_args(
        source_agent="my-agent",
        remaining=["new-agent", "--provider", "docker", "--model", "opus"],
        original_argv=[
            "mngr",
            "clone",
            "my-agent",
            "new-agent",
            "--provider",
            "docker",
            "--",
            "--model",
            "opus",
        ],
    )
    assert result == [
        "--from",
        "my-agent",
        "new-agent",
        "--provider",
        "docker",
        "--",
        "--model",
        "opus",
    ]


def test_build_create_args_with_double_dash_and_empty_remaining() -> None:
    """A trailing -- with no args after it is preserved."""
    result = _build_create_args(
        source_agent="my-agent",
        remaining=[],
        original_argv=["mngr", "clone", "my-agent", "--"],
    )
    assert result == ["--from", "my-agent", "--"]


# --- _args_before_dd_count tests ---


def test_args_before_dd_count_no_dd() -> None:
    """Returns None when -- is not in original_argv."""
    assert _args_before_dd_count(["--provider", "docker"], ["mngr", "clone", "a", "--provider", "docker"]) is None


def test_args_before_dd_count_with_dd() -> None:
    """Returns count of args before -- boundary."""
    count = _args_before_dd_count(
        ["--provider", "docker", "--model", "opus"],
        ["mngr", "clone", "a", "--provider", "docker", "--", "--model", "opus"],
    )
    assert count == 2


def test_args_before_dd_count_trailing_dd() -> None:
    """Returns full length when -- has nothing after it."""
    count = _args_before_dd_count(
        ["--provider", "docker"],
        ["mngr", "clone", "a", "--provider", "docker", "--"],
    )
    assert count == 2


# --- _reject_source_agent_options with -- boundary tests ---


def test_reject_source_agent_options_allows_from_after_dd() -> None:
    """--from after -- should not be rejected."""
    ctx = click.Context(clone, info_name="clone")
    # before_dd=0 means nothing is before --, so --from is after --
    _reject_source_agent_options(["--from", "x"], ctx, before_dd=0)


def test_reject_source_agent_options_rejects_from_before_dd() -> None:
    """--from before -- should still be rejected."""
    ctx = click.Context(clone, info_name="clone")
    with pytest.raises(click.UsageError, match="--from"):
        _reject_source_agent_options(["--from", "x", "--model", "opus"], ctx, before_dd=2)
