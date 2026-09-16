"""Unit tests for the pair CLI command."""

import json
import os
import signal
from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_pair.api import UnisonSyncer
from imbue.mngr_pair.cli import PairCliOptions
from imbue.mngr_pair.cli import SyncStopSignal
from imbue.mngr_pair.cli import _emit_pair_started
from imbue.mngr_pair.cli import _emit_pair_stopped
from imbue.mngr_pair.cli import _emit_pair_syncing
from imbue.mngr_pair.cli import _resolve_source
from imbue.mngr_pair.cli import _sigterm_requests_stop
from imbue.mngr_pair.cli import pair
from imbue.mngr_pair.remote import UnisonRoot


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


@pytest.mark.parametrize("output_format", [OutputFormat.HUMAN, OutputFormat.JSON, OutputFormat.JSONL])
def test_emit_pair_syncing_exercises_all_format_branches(
    output_format: OutputFormat,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_pair_syncing announces the live sync in each format."""
    output_opts = OutputOptions(output_format=output_format)
    _emit_pair_syncing(output_opts)
    captured = capsys.readouterr()
    if output_format == OutputFormat.HUMAN:
        assert "Sync started" in captured.out
    elif output_format == OutputFormat.JSONL:
        assert json.loads(captured.out)["event"] == "pair_syncing"
    else:
        assert captured.out == ""


def _stop_signal(cg: ConcurrencyGroup) -> SyncStopSignal:
    """A stop signal over a syncer that was never started, which it never touches."""
    return SyncStopSignal(
        syncer=UnisonSyncer(
            source_root=UnisonRoot(path=Path("/src")),
            target_root=UnisonRoot(path=Path("/dst")),
            cg=cg,
        )
    )


def test_sigterm_asks_the_sync_to_stop_instead_of_raising() -> None:
    """The handler must not raise.

    Raising out of it lands the exception inside the ``Thread.join`` the
    command blocks in, and CPython then marks that thread stopped while it is
    still running -- after which the teardown never signals unison. Nothing is
    raised here, so the join, and the teardown behind it, stay intact.
    """
    with ConcurrencyGroup(name="pair-cli-test") as cg:
        stop_signal = _stop_signal(cg)
        with _sigterm_requests_stop(stop_signal):
            os.kill(os.getpid(), signal.SIGTERM)
            stop_signal.wait()
        assert stop_signal.is_stop_requested
        # Nothing ended on its own, so there is no exit code to report on.
        assert stop_signal.exit_code is None


def test_the_previous_sigterm_handler_is_restored_on_the_way_out() -> None:
    with ConcurrencyGroup(name="pair-cli-test") as cg:
        before = signal.getsignal(signal.SIGTERM)
        with _sigterm_requests_stop(_stop_signal(cg)):
            assert signal.getsignal(signal.SIGTERM) is not before
        assert signal.getsignal(signal.SIGTERM) is before


def _host_only_options(
    *, source_path: str | None, is_require_git: bool = False, is_start: bool = True
) -> PairCliOptions:
    """Options naming a host and no agent, which is what pairing with a host looks like."""
    return PairCliOptions(
        source_pos=None,
        source=None,
        source_agent=None,
        source_host=HostAddress(host=HostName("some-host"), provider=None),
        source_path=source_path,
        target=None,
        require_git=is_require_git,
        ignore_archives=False,
        links=True,
        start=is_start,
        sync_direction="both",
        conflict="newer",
        uncommitted_changes="fail",
        include=(),
        exclude=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )


def test_pairing_with_a_host_refuses_a_relative_source_path() -> None:
    """Without an agent there is no work directory for a relative path to be relative to."""
    with pytest.raises(UserInputError, match="absolute --source-path"):
        _resolve_source(
            source_address=None,
            source_subpath=Path("relative/dir"),
            opts=_host_only_options(source_path="relative/dir"),
            mngr_ctx=None,  # ty: ignore[invalid-argument-type]
        )


def test_pairing_with_a_host_refuses_no_source_path_at_all() -> None:
    with pytest.raises(UserInputError, match="absolute --source-path"):
        _resolve_source(
            source_address=None,
            source_subpath=None,
            opts=_host_only_options(source_path=None),
            mngr_ctx=None,  # ty: ignore[invalid-argument-type]
        )


def test_pairing_with_a_host_refuses_git_sync() -> None:
    """Git sync reconciles an agent's repository, so a host on its own cannot do it."""
    with pytest.raises(UserInputError, match="needs an agent"):
        _resolve_source(
            source_address=None,
            source_subpath=Path("/absolute/dir"),
            opts=_host_only_options(source_path="/absolute/dir", is_require_git=True),
            mngr_ctx=None,  # ty: ignore[invalid-argument-type]
        )


def test_pair_help_offers_the_standard_start_flag() -> None:
    """Pairing starts an offline host by default, and --no-start is how a caller declines."""
    result = CliRunner().invoke(pair, ["--help"])
    assert result.exit_code == 0
    assert "--start" in result.output
    assert "--no-start" in result.output
