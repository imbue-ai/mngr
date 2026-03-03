"""Unit tests for the clone CLI command."""

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.clone import _args_before_dd_count
from imbue.mng.cli.clone import _build_create_args
from imbue.mng.cli.clone import _reject_source_agent_options
from imbue.mng.cli.clone import clone
from imbue.mng.main import cli


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


def test_clone_rejects_from_agent_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Clone should reject --from-agent in remaining args."""
    result = cli_runner.invoke(
        clone,
        ["source-agent", "--from-agent", "other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--from-agent" in result.output


def test_clone_rejects_source_agent_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Clone should reject --source-agent in remaining args."""
    result = cli_runner.invoke(
        clone,
        ["source-agent", "--source-agent", "other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--source-agent" in result.output


def test_clone_rejects_from_agent_equals_form(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Clone should reject --from-agent=value form in remaining args."""
    result = cli_runner.invoke(
        clone,
        ["source-agent", "--from-agent=other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--from-agent" in result.output


# --- _build_create_args tests ---


def test_build_create_args_without_double_dash() -> None:
    """Without -- in argv, remaining args are passed through directly."""
    result = _build_create_args(
        source_agent="my-agent",
        remaining=["--in", "docker"],
        original_argv=["mng", "clone", "my-agent", "--in", "docker"],
    )
    assert result == ["--from-agent", "my-agent", "--in", "docker"]


def test_build_create_args_with_double_dash() -> None:
    """With -- in argv, the separator is re-inserted in create_args."""
    result = _build_create_args(
        source_agent="my-agent",
        remaining=["--model", "opus"],
        original_argv=["mng", "clone", "my-agent", "--", "--model", "opus"],
    )
    assert result == ["--from-agent", "my-agent", "--", "--model", "opus"]


def test_build_create_args_with_create_options_and_double_dash() -> None:
    """Create options before -- and agent args after -- are split correctly."""
    result = _build_create_args(
        source_agent="my-agent",
        remaining=["new-agent", "--in", "docker", "--model", "opus"],
        original_argv=[
            "mng",
            "clone",
            "my-agent",
            "new-agent",
            "--in",
            "docker",
            "--",
            "--model",
            "opus",
        ],
    )
    assert result == [
        "--from-agent",
        "my-agent",
        "new-agent",
        "--in",
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
        original_argv=["mng", "clone", "my-agent", "--"],
    )
    assert result == ["--from-agent", "my-agent", "--"]


# --- _args_before_dd_count tests ---


def test_args_before_dd_count_no_dd() -> None:
    """Returns None when -- is not in original_argv."""
    assert _args_before_dd_count(["--in", "docker"], ["mng", "clone", "a", "--in", "docker"]) is None


def test_args_before_dd_count_with_dd() -> None:
    """Returns count of args before -- boundary."""
    count = _args_before_dd_count(
        ["--in", "docker", "--model", "opus"],
        ["mng", "clone", "a", "--in", "docker", "--", "--model", "opus"],
    )
    assert count == 2


def test_args_before_dd_count_trailing_dd() -> None:
    """Returns full length when -- has nothing after it."""
    count = _args_before_dd_count(
        ["--in", "docker"],
        ["mng", "clone", "a", "--in", "docker", "--"],
    )
    assert count == 2


# --- _reject_source_agent_options with -- boundary tests ---


def test_reject_source_agent_options_allows_from_agent_after_dd() -> None:
    """--from-agent after -- should not be rejected."""
    ctx = click.Context(clone, info_name="clone")
    # before_dd=0 means nothing is before --, so --from-agent is after --
    _reject_source_agent_options(["--from-agent", "x"], ctx, before_dd=0)


def test_reject_source_agent_options_rejects_from_agent_before_dd() -> None:
    """--from-agent before -- should still be rejected."""
    ctx = click.Context(clone, info_name="clone")
    with pytest.raises(click.UsageError, match="--from-agent"):
        _reject_source_agent_options(["--from-agent", "x", "--model", "opus"], ctx, before_dd=2)
