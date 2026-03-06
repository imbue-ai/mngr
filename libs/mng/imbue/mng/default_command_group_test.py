from pathlib import Path

import click
import pluggy
import pytest
from click.shell_completion import CompletionItem
from click.shell_completion import ShellComplete
from click.testing import CliRunner

from imbue.mng.cli.default_command_group import DefaultCommandGroup
from imbue.mng.cli.snapshot import snapshot
from imbue.mng.main import cli

# =============================================================================
# DefaultCommandGroup tests
# =============================================================================
#
# These tests exercise the default-to-create and unrecognized-command-forwarding
# behavior using a minimal group with "create" and "list" subcommands.
# Commands record their invocation info in a shared dict so tests can verify routing.


def _make_test_group(invocation_record: dict[str, str | None]) -> click.Group:
    """Build a minimal DefaultCommandGroup with 'create' and 'list' subcommands."""

    @click.group(cls=DefaultCommandGroup)
    def group() -> None:
        pass

    @group.command(name="create")
    @click.argument("name", required=False)
    def create_cmd(name: str | None) -> None:
        invocation_record["command"] = "create"
        invocation_record["name"] = name

    @group.command(name="list")
    def list_cmd() -> None:
        invocation_record["command"] = "list"

    return group


def test_bare_invocation_defaults_to_create() -> None:
    """Running the group with no args should forward to 'create'."""
    record: dict[str, str | None] = {}
    group = _make_test_group(record)
    runner = CliRunner()
    result = runner.invoke(group, [])
    assert result.exit_code == 0
    assert record["command"] == "create"


def test_unrecognized_command_forwards_to_create() -> None:
    """Running the group with an unrecognized command should forward to 'create'."""
    record: dict[str, str | None] = {}
    group = _make_test_group(record)
    runner = CliRunner()
    result = runner.invoke(group, ["my-task"])
    assert result.exit_code == 0
    assert record["command"] == "create"
    assert record["name"] == "my-task"


def test_recognized_command_not_forwarded() -> None:
    """Running the group with a recognized command should NOT be forwarded to create."""
    record: dict[str, str | None] = {}
    group = _make_test_group(record)
    runner = CliRunner()
    result = runner.invoke(group, ["list"])
    assert result.exit_code == 0
    assert record["command"] == "list"


def test_explicit_create_still_works() -> None:
    """Running 'create' explicitly should still work normally."""
    record: dict[str, str | None] = {}
    group = _make_test_group(record)
    runner = CliRunner()
    result = runner.invoke(group, ["create", "my-agent"])
    assert result.exit_code == 0
    assert record["command"] == "create"
    assert record["name"] == "my-agent"


# =============================================================================
# Integration tests: real mng CLI defaults to create
# =============================================================================


def test_mng_bare_invocation_defaults_to_create(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo_cwd: Path,
) -> None:
    """Running `mng` with no args should forward to `mng create`."""
    result = cli_runner.invoke(cli, [], obj=plugin_manager)
    # create with no args should attempt to create an agent (not show group help)
    assert "Missing command" not in result.output
    assert "Commands:" not in result.output


def test_mng_unrecognized_command_forwards_to_create(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_git_repo_cwd: Path,
) -> None:
    """Running `mng my-task` should forward to `mng create my-task`."""
    result = cli_runner.invoke(cli, ["my-task"], obj=plugin_manager)
    assert "No such command" not in result.output


def test_mng_snapshot_bare_defaults_to_create(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mng snapshot` with no args should forward to `snapshot create`."""
    result = cli_runner.invoke(snapshot, [], obj=plugin_manager)
    assert "Missing command" not in result.output
    assert "Commands:" not in result.output
    assert "Must specify at least one agent" in result.output


def test_mng_snapshot_unrecognized_forwards_to_create(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mng snapshot nonexistent` should forward to `snapshot create nonexistent`."""
    result = cli_runner.invoke(snapshot, ["nonexistent"], obj=plugin_manager)
    assert "No such command" not in result.output


# =============================================================================
# Configurable default command tests
# =============================================================================


def _make_config_key_group(
    invocation_record: dict[str, str | None],
    config_key: str,
) -> click.Group:
    """Build a DefaultCommandGroup with a _config_key and 'create', 'list', 'stop' subcommands."""

    class _TestGroup(DefaultCommandGroup):
        _config_key = config_key

    @click.group(cls=_TestGroup)
    def group() -> None:
        pass

    @group.command(name="create")
    @click.argument("name", required=False)
    def create_cmd(name: str | None) -> None:
        invocation_record["command"] = "create"
        invocation_record["name"] = name

    @group.command(name="list")
    def list_cmd() -> None:
        invocation_record["command"] = "list"

    @group.command(name="stop")
    def stop_cmd() -> None:
        invocation_record["command"] = "stop"

    return group


def test_config_key_custom_default(
    monkeypatch: pytest.MonkeyPatch,
    project_config_dir: Path,
    temp_git_repo: Path,
) -> None:
    """A group with _config_key should use default_subcommand from config."""
    (project_config_dir / "settings.toml").write_text('[commands.testgrp]\ndefault_subcommand = "list"\n')
    monkeypatch.chdir(temp_git_repo)

    record: dict[str, str | None] = {}
    group = _make_config_key_group(record, config_key="testgrp")
    runner = CliRunner()
    result = runner.invoke(group, [])
    assert result.exit_code == 0
    assert record["command"] == "list"


def test_config_key_disabled_shows_help(
    monkeypatch: pytest.MonkeyPatch,
    project_config_dir: Path,
    temp_git_repo: Path,
) -> None:
    """When default_subcommand is empty string, bare invocation shows help."""
    (project_config_dir / "settings.toml").write_text('[commands.testgrp]\ndefault_subcommand = ""\n')
    monkeypatch.chdir(temp_git_repo)

    record: dict[str, str | None] = {}
    group = _make_config_key_group(record, config_key="testgrp")
    runner = CliRunner()
    result = runner.invoke(group, [])
    assert "Commands:" in result.output or "Usage:" in result.output
    assert "command" not in record


def test_config_key_disabled_unrecognized_errors(
    monkeypatch: pytest.MonkeyPatch,
    project_config_dir: Path,
    temp_git_repo: Path,
) -> None:
    """When default_subcommand is empty string, unrecognized command shows error."""
    (project_config_dir / "settings.toml").write_text('[commands.testgrp]\ndefault_subcommand = ""\n')
    monkeypatch.chdir(temp_git_repo)

    record: dict[str, str | None] = {}
    group = _make_config_key_group(record, config_key="testgrp")
    runner = CliRunner()
    result = runner.invoke(group, ["nonexistent"])
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_no_config_key_uses_default_command_attribute() -> None:
    """A group without _config_key should use the _default_command attribute."""
    record: dict[str, str | None] = {}
    # _make_test_group creates a plain DefaultCommandGroup (no _config_key)
    group = _make_test_group(record)
    runner = CliRunner()
    result = runner.invoke(group, [])
    assert result.exit_code == 0
    assert record["command"] == "create"


# =============================================================================
# Tab completion tests
# =============================================================================


def test_tab_completion_lists_subcommands() -> None:
    """Tab completing after the group name should list subcommands, not default to create."""
    completions = _get_completions(_make_test_group({}), [], "")
    assert {"create", "list"} == {c.value for c in completions}


def test_tab_completion_filters_by_prefix() -> None:
    """Tab completing with a partial subcommand name should filter matches."""
    completions = _get_completions(_make_test_group({}), [], "cr")
    assert {c.value for c in completions} == {"create"}


def test_tab_completion_after_subcommand_does_not_list_subcommands() -> None:
    """Tab completing after a resolved subcommand should not list sibling subcommands."""
    completions = _get_completions(_make_test_group({}), ["create"], "")
    # Should not contain sibling subcommands
    assert "list" not in {c.value for c in completions}


def _get_completions(group: click.Group, args: list[str], incomplete: str) -> list[CompletionItem]:
    return ShellComplete(group, {}, "test", "_TEST_COMPLETE").get_completions(args, incomplete)
