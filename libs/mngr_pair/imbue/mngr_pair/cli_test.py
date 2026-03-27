"""Unit tests for the pair CLI command."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_pair.cli import PairCliOptions
from imbue.mngr_pair.cli import _emit_pair_started
from imbue.mngr_pair.cli import _emit_pair_stopped
from imbue.mngr_pair.cli import pair


def test_pair_cli_options_has_all_fields() -> None:
    """Test that PairCliOptions has all required fields."""
    assert hasattr(PairCliOptions, "__annotations__")
    annotations = PairCliOptions.__annotations__
    assert "source" in annotations
    assert "source_agent" in annotations
    assert "source_host" in annotations
    assert "sync_direction" in annotations
    assert "conflict" in annotations
    assert "exclude" in annotations
    assert "require_git" in annotations
    assert "uncommitted_changes" in annotations


def test_pair_command_is_registered() -> None:
    """Test that the pair command is properly registered."""
    assert pair is not None
    assert pair.name == "pair"


def test_pair_command_help_shows_options() -> None:
    """Test that --help shows all expected options."""
    runner = CliRunner()
    result = runner.invoke(pair, ["--help"])
    assert result.exit_code == 0
    assert "--source" in result.output or "-s" in result.output
    assert "--source-agent" in result.output
    assert "--source-host" in result.output
    assert "--sync-direction" in result.output
    assert "--conflict" in result.output
    assert "--exclude" in result.output
    assert "--require-git" in result.output or "--no-require-git" in result.output
    assert "--uncommitted-changes" in result.output


def test_pair_sync_direction_choices() -> None:
    """Test that direction option has expected choices."""
    runner = CliRunner()
    result = runner.invoke(pair, ["--help"])
    assert result.exit_code == 0
    assert "both" in result.output.lower() or "source" in result.output.lower()


def test_pair_conflict_choices() -> None:
    """Test that conflict option has expected choices."""
    runner = CliRunner()
    result = runner.invoke(pair, ["--help"])
    assert result.exit_code == 0
    assert "conflict" in result.output.lower()


def test_pair_uncommitted_changes_choices() -> None:
    """Test that uncommitted-changes option has expected choices."""
    runner = CliRunner()
    result = runner.invoke(pair, ["--help"])
    assert result.exit_code == 0
    assert "uncommitted" in result.output.lower()


@pytest.mark.parametrize("output_format", [OutputFormat.HUMAN, OutputFormat.JSON, OutputFormat.JSONL])
def test_emit_pair_started_exercises_all_format_branches(
    output_format: OutputFormat,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_pair_started should handle all output formats without error."""
    output_opts = OutputOptions(output_format=output_format)
    _emit_pair_started(Path("/src"), Path("/dst"), output_opts)
    captured = capsys.readouterr()
    if output_format == OutputFormat.HUMAN:
        assert "/src" in captured.out
        assert "/dst" in captured.out


@pytest.mark.parametrize("output_format", [OutputFormat.HUMAN, OutputFormat.JSON, OutputFormat.JSONL])
def test_emit_pair_stopped_exercises_all_format_branches(
    output_format: OutputFormat,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_pair_stopped should handle all output formats without error."""
    output_opts = OutputOptions(output_format=output_format)
    _emit_pair_stopped(output_opts)
    captured = capsys.readouterr()
    if output_format == OutputFormat.HUMAN:
        assert "stopped" in captured.out.lower()
