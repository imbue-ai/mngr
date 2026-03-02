import json

import pluggy
from click.testing import CliRunner

from imbue.mng.cli.rename import RenameCliOptions
from imbue.mng.cli.rename import _output
from imbue.mng.cli.rename import _output_result
from imbue.mng.cli.rename import rename
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat


def test_rename_cli_options_parsing_creates_valid_options() -> None:
    """Test that RenameCliOptions can be constructed with the expected fields."""
    opts = RenameCliOptions(
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
        current="my-agent",
        new_name="new-agent",
        dry_run=False,
        host=False,
    )
    assert opts.current == "my-agent"
    assert opts.new_name == "new-agent"
    assert opts.dry_run is False


def test_rename_cli_options_with_dry_run() -> None:
    """Test RenameCliOptions with dry_run enabled."""
    opts = RenameCliOptions(
        output_format="json",
        quiet=True,
        verbose=1,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
        current="agent-123",
        new_name="renamed-agent",
        dry_run=True,
        host=False,
    )
    assert opts.current == "agent-123"
    assert opts.new_name == "renamed-agent"
    assert opts.dry_run is True
    assert opts.output_format == "json"
    assert opts.quiet is True


def _make_output_opts(fmt: OutputFormat = OutputFormat.HUMAN) -> OutputOptions:
    return OutputOptions(output_format=fmt, format_template=None)


def test_rename_output_human_format(capsys) -> None:
    """_output should write to stdout in HUMAN format."""
    _output("Renamed agent", _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "Renamed agent" in captured.out


def test_rename_output_json_format(capsys) -> None:
    """_output should be silent in JSON format."""
    _output("Renamed agent", _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    assert captured.out == ""


def test_rename_output_result_human(capsys) -> None:
    """_output_result with HUMAN should show rename message."""
    _output_result("old", "new", "agent-id", _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "old" in captured.out
    assert "new" in captured.out


def test_rename_output_result_json(capsys) -> None:
    """_output_result with JSON should emit JSON."""
    _output_result("old", "new", "agent-id", _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["old_name"] == "old"
    assert output["new_name"] == "new"


def test_rename_output_result_jsonl(capsys) -> None:
    """_output_result with JSONL should emit event."""
    _output_result("old", "new", "agent-id", _make_output_opts(OutputFormat.JSONL))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "rename_result"


def test_rename_requires_two_arguments(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that rename requires both current and new name arguments."""
    result = cli_runner.invoke(
        rename,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_rename_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test renaming a non-existent agent returns error."""
    result = cli_runner.invoke(
        rename,
        ["nonexistent-agent-99812", "new-name"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_rename_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --help exits 0."""
    result = cli_runner.invoke(
        rename,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
