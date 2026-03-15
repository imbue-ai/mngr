import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.label import LabelCliOptions
from imbue.mng.cli.label import _merge_labels
from imbue.mng.cli.label import _output
from imbue.mng.cli.label import _output_result
from imbue.mng.cli.label import label
from imbue.mng.cli.label import parse_label_string
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import OutputFormat


def _make_output_opts(fmt: OutputFormat = OutputFormat.HUMAN) -> OutputOptions:
    return OutputOptions(output_format=fmt, format_template=None)


def test_label_cli_options_parsing() -> None:
    """Test that LabelCliOptions can be constructed with expected fields."""
    opts = LabelCliOptions(
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
        agents=("agent-1",),
        agent_list=(),
        label=("key=value",),
        label_all=False,
        dry_run=False,
    )
    assert opts.agents == ("agent-1",)
    assert opts.label == ("key=value",)
    assert opts.dry_run is False


def test_parse_label_string_valid() -> None:
    """parse_label_string should split KEY=VALUE correctly."""
    key, value = parse_label_string("archived_at=2026-03-15")
    assert key == "archived_at"
    assert value == "2026-03-15"


def test_parse_label_string_value_with_equals() -> None:
    """parse_label_string should handle values that contain equals signs."""
    key, value = parse_label_string("note=a=b=c")
    assert key == "note"
    assert value == "a=b=c"


def test_parse_label_string_empty_value() -> None:
    """parse_label_string should accept empty values."""
    key, value = parse_label_string("status=")
    assert key == "status"
    assert value == ""


def test_parse_label_string_no_equals() -> None:
    """parse_label_string should raise on missing equals sign."""
    with pytest.raises(UserInputError, match="KEY=VALUE"):
        parse_label_string("noequalssign")


def test_parse_label_string_empty_key() -> None:
    """parse_label_string should raise on empty key."""
    with pytest.raises(UserInputError, match="key cannot be empty"):
        parse_label_string("=value")


def test_merge_labels_adds_new() -> None:
    """_merge_labels should add new keys."""
    result = _merge_labels({"a": "1"}, {"b": "2"})
    assert result == {"a": "1", "b": "2"}


def test_merge_labels_overwrites_existing() -> None:
    """_merge_labels should overwrite existing keys."""
    result = _merge_labels({"a": "1", "b": "2"}, {"a": "updated"})
    assert result == {"a": "updated", "b": "2"}


def test_merge_labels_empty_current() -> None:
    """_merge_labels should work with empty current labels."""
    result = _merge_labels({}, {"key": "value"})
    assert result == {"key": "value"}


def test_output_human(capsys) -> None:
    """_output should write message to stdout in HUMAN format."""
    _output("test message", _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "test message" in captured.out


def test_output_json_silent(capsys) -> None:
    """_output should be silent in JSON format."""
    _output("test message", _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    assert captured.out == ""


def test_output_result_human(capsys) -> None:
    """_output_result in HUMAN format shows change count."""
    changes = [{"agent_name": "a", "labels": {"k": "v"}}]
    _output_result(changes, _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "1 agent(s)" in captured.out


def test_output_result_json(capsys) -> None:
    """_output_result in JSON format emits structured JSON."""
    changes = [{"agent_name": "a", "labels": {"k": "v"}}]
    _output_result(changes, _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["count"] == 1
    assert len(output["changes"]) == 1


def test_output_result_jsonl(capsys) -> None:
    """_output_result in JSONL format emits event with data."""
    changes = [{"agent_name": "a", "labels": {"k": "v"}}]
    _output_result(changes, _make_output_opts(OutputFormat.JSONL))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "label_result"
    assert output["count"] == 1


def test_output_result_empty_changes(capsys) -> None:
    """_output_result in HUMAN format should be silent when no changes."""
    _output_result([], _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert captured.out == ""


def test_label_requires_label_flag(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """label command should fail when no --label is provided."""
    result = cli_runner.invoke(
        label,
        ["my-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_label_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """label command should fail when no agent is specified and --all is not used."""
    result = cli_runner.invoke(
        label,
        ["--label", "key=value"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_label_agents_and_all_conflict(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """label command should fail when both agents and --all are provided."""
    result = cli_runner.invoke(
        label,
        ["my-agent", "--all", "--label", "key=value"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
