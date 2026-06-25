"""Unit tests for the pair CLI command."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_pair.cli import _emit_pair_started
from imbue.mngr_pair.cli import _emit_pair_stopped
from imbue.mngr_pair.cli import pair


def test_pair_command_help_shows_all_options() -> None:
    """--help should list every option the command accepts."""
    runner = CliRunner()
    result = runner.invoke(pair, ["--help"])
    assert result.exit_code == 0
    for option in (
        "--source",
        "--source-agent",
        "--source-host",
        "--source-path",
        "--target",
        "--sync-direction",
        "--conflict",
        "--uncommitted-changes",
        "--include",
        "--exclude",
        "--require-git",
        "--no-require-git",
    ):
        assert option in result.output


@pytest.mark.parametrize(
    ("option_name", "expected_choice_metavar"),
    [
        ("--sync-direction", "[both|forward|reverse]"),
        ("--conflict", "[newer|source|target|ask]"),
        ("--uncommitted-changes", "[stash|clobber|merge|fail]"),
    ],
)
def test_pair_help_lists_every_choice_for_choice_options(option_name: str, expected_choice_metavar: str) -> None:
    """Each choice option's help must list its full, exact set of allowed values.

    Asserting the rendered ``[a|b|c]`` metavar (rather than just that the option
    name appears) means dropping or misspelling any single choice in the
    ``click.Choice(...)`` lists in cli.py fails this test.
    """
    runner = CliRunner()
    result = runner.invoke(pair, ["--help"])
    assert result.exit_code == 0
    assert expected_choice_metavar in result.output


@pytest.mark.parametrize("output_format", [OutputFormat.HUMAN, OutputFormat.JSON, OutputFormat.JSONL])
def test_emit_pair_started_emits_expected_output_for_each_format(
    output_format: OutputFormat,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_pair_started should emit the right content for each output format."""
    output_opts = OutputOptions(output_format=output_format)
    _emit_pair_started(Path("/src"), Path("/dst"), output_opts)
    captured = capsys.readouterr()
    match output_format:
        case OutputFormat.HUMAN:
            assert "/src" in captured.out
            assert "/dst" in captured.out
        case OutputFormat.JSONL:
            event = json.loads(captured.out)
            assert event["event"] == "pair_started"
            assert event["source_path"] == "/src"
            assert event["target_path"] == "/dst"
        case OutputFormat.JSON:
            # JSON mode defers all output until the command's final result, so
            # an intermediate event emits nothing.
            assert captured.out == ""


@pytest.mark.parametrize("output_format", [OutputFormat.HUMAN, OutputFormat.JSON, OutputFormat.JSONL])
def test_emit_pair_stopped_emits_expected_output_for_each_format(
    output_format: OutputFormat,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_pair_stopped should emit the right content for each output format."""
    output_opts = OutputOptions(output_format=output_format)
    _emit_pair_stopped(output_opts)
    captured = capsys.readouterr()
    match output_format:
        case OutputFormat.HUMAN:
            assert "stopped" in captured.out.lower()
        case OutputFormat.JSONL:
            event = json.loads(captured.out)
            assert event["event"] == "pair_stopped"
        case OutputFormat.JSON:
            # JSON mode defers all output until the command's final result.
            assert captured.out == ""
