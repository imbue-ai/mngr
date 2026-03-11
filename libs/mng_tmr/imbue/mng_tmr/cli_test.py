"""Unit tests for tmr CLI."""

from pathlib import Path

from click.testing import CliRunner

from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng_tmr.cli import _emit_agents_launched
from imbue.mng_tmr.cli import _emit_report_path
from imbue.mng_tmr.cli import _emit_test_count
from imbue.mng_tmr.cli import _split_pytest_args
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


def test_split_pytest_args_no_separator() -> None:
    pos, flags = _split_pytest_args(("tests/e2e", "tests/unit"))
    assert pos == ("tests/e2e", "tests/unit")
    assert flags == ()


def test_split_pytest_args_with_separator() -> None:
    pos, flags = _split_pytest_args(("tests/e2e", "--", "-m", "release"))
    assert pos == ("tests/e2e",)
    assert flags == ("-m", "release")


def test_split_pytest_args_only_flags() -> None:
    pos, flags = _split_pytest_args(("--", "-m", "release", "-v"))
    assert pos == ()
    assert flags == ("-m", "release", "-v")


def test_split_pytest_args_empty() -> None:
    pos, flags = _split_pytest_args(())
    assert pos == ()
    assert flags == ()


def test_split_pytest_args_separator_only() -> None:
    pos, flags = _split_pytest_args(("--",))
    assert pos == ()
    assert flags == ()


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


def test_cli_help_contains_timeout_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--timeout" in result.output
    assert "--integrator-timeout" in result.output
