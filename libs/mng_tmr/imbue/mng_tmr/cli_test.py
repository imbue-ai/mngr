"""Unit tests for tmr CLI."""

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng_tmr.cli import _TmrCommand
from imbue.mng_tmr.cli import _emit_agents_launched
from imbue.mng_tmr.cli import _emit_report_path
from imbue.mng_tmr.cli import _emit_test_count
from imbue.mng_tmr.cli import _parse_label_options
from imbue.mng_tmr.cli import tmr


def test_cli_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert result.exit_code == 0
    assert "PYTEST_ARGS" in result.output


def test_cli_help_contains_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--agent-type" in result.output
    assert "--poll-interval" in result.output
    assert "--output-html" in result.output
    assert "--source" in result.output


def test_cli_help_contains_new_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--provider" in result.output
    assert "--env" in result.output
    assert "--label" in result.output
    assert "--prompt-suffix" in result.output


def test_cli_help_contains_timeout_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--timeout" in result.output
    assert "--integrator-timeout" in result.output


def _human_output_opts() -> OutputOptions:
    return OutputOptions(output_format=OutputFormat.HUMAN)


def test_emit_test_count_human(capsys: object) -> None:
    _emit_test_count(5, _human_output_opts())


def test_emit_agents_launched_human(capsys: object) -> None:
    _emit_agents_launched(3, _human_output_opts())


def test_emit_report_path_human(capsys: object, tmp_path: object) -> None:
    _emit_report_path(Path("/tmp/report.html"), _human_output_opts())


def test_emit_test_count_json() -> None:
    _emit_test_count(10, OutputOptions(output_format=OutputFormat.JSON))


def test_emit_agents_launched_jsonl() -> None:
    _emit_agents_launched(7, OutputOptions(output_format=OutputFormat.JSONL))


def test_emit_report_path_json() -> None:
    _emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSON))


def test_emit_report_path_jsonl() -> None:
    _emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSONL))


def test_emit_test_count_jsonl() -> None:
    _emit_test_count(3, OutputOptions(output_format=OutputFormat.JSONL))


def test_emit_agents_launched_json() -> None:
    _emit_agents_launched(5, OutputOptions(output_format=OutputFormat.JSON))


def test_tmr_command_splits_on_double_dash() -> None:
    """_TmrCommand correctly captures args after -- as testing_flags."""
    captured: dict[str, object] = {}

    @click.command(cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
    @click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def dummy_cmd(ctx: click.Context, **kwargs: object) -> None:
        captured["pytest_args"] = kwargs["pytest_args"]
        captured["testing_flags"] = kwargs["testing_flags"]

    runner = CliRunner()
    result = runner.invoke(dummy_cmd, ["tests/e2e", "--", "-m", "release"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ("tests/e2e",)
    assert captured["testing_flags"] == ("-m", "release")


def test_tmr_command_no_separator() -> None:
    """Without --, all args go into pytest_args and testing_flags is empty."""
    captured: dict[str, object] = {}

    @click.command(cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
    @click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def dummy_cmd(ctx: click.Context, **kwargs: object) -> None:
        captured["pytest_args"] = kwargs["pytest_args"]
        captured["testing_flags"] = kwargs["testing_flags"]

    runner = CliRunner()
    result = runner.invoke(dummy_cmd, ["tests/e2e", "tests/unit"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ("tests/e2e", "tests/unit")
    assert captured["testing_flags"] == ()


def test_tmr_command_only_flags() -> None:
    """-- with nothing before it gives empty pytest_args."""
    captured: dict[str, object] = {}

    @click.command(cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
    @click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def dummy_cmd(ctx: click.Context, **kwargs: object) -> None:
        captured["pytest_args"] = kwargs["pytest_args"]
        captured["testing_flags"] = kwargs["testing_flags"]

    runner = CliRunner()
    result = runner.invoke(dummy_cmd, ["--", "-m", "release", "-v"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ()
    assert captured["testing_flags"] == ("-m", "release", "-v")


def test_tmr_command_separator_only() -> None:
    """Just -- gives empty args and empty flags."""
    captured: dict[str, object] = {}

    @click.command(cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
    @click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def dummy_cmd(ctx: click.Context, **kwargs: object) -> None:
        captured["pytest_args"] = kwargs["pytest_args"]
        captured["testing_flags"] = kwargs["testing_flags"]

    runner = CliRunner()
    result = runner.invoke(dummy_cmd, ["--"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ()
    assert captured["testing_flags"] == ()


def test_tmr_command_options_before_separator() -> None:
    """Known options before -- are parsed normally, not captured as args."""
    captured: dict[str, object] = {}

    @click.command(cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
    @click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
    @click.option("--provider", default="local")
    @click.pass_context
    def dummy_cmd(ctx: click.Context, **kwargs: object) -> None:
        captured["pytest_args"] = kwargs["pytest_args"]
        captured["testing_flags"] = kwargs["testing_flags"]
        captured["provider"] = kwargs["provider"]

    runner = CliRunner()
    result = runner.invoke(dummy_cmd, ["--provider", "docker", "tests/", "--", "-m", "release"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ("tests/",)
    assert captured["testing_flags"] == ("-m", "release")
    assert captured["provider"] == "docker"


def test_parse_label_options_valid() -> None:
    result = _parse_label_options(("key=value", "batch=run1"))
    assert result == AgentLabelOptions(labels={"key": "value", "batch": "run1"})


def test_parse_label_options_empty() -> None:
    result = _parse_label_options(())
    assert result == AgentLabelOptions(labels={})


def test_parse_label_options_invalid_raises() -> None:
    with pytest.raises(UserInputError):
        _parse_label_options(("no-equals-sign",))
