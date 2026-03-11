"""Unit tests for tmr CLI."""

from pathlib import Path

from click.testing import CliRunner

from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng_test_mapreduce.cli import _emit_agents_launched
from imbue.mng_test_mapreduce.cli import _emit_report_path
from imbue.mng_test_mapreduce.cli import _emit_test_count
from imbue.mng_test_mapreduce.cli import tmr


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


def _human_output_opts() -> OutputOptions:
    return OutputOptions(output_format=OutputFormat.HUMAN)


def test_emit_test_count_human(capsys: object) -> None:
    """Smoke test: does not raise."""
    _emit_test_count(5, _human_output_opts())


def test_emit_agents_launched_human(capsys: object) -> None:
    """Smoke test: does not raise."""
    _emit_agents_launched(3, _human_output_opts())


def test_emit_report_path_human(capsys: object, tmp_path: object) -> None:
    """Smoke test: does not raise."""
    _emit_report_path(Path("/tmp/report.html"), _human_output_opts())


def test_emit_test_count_json() -> None:
    """Smoke test: JSON format does not raise."""
    opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_test_count(10, opts)


def test_emit_agents_launched_jsonl() -> None:
    """Smoke test: JSONL format does not raise."""
    opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_agents_launched(7, opts)
