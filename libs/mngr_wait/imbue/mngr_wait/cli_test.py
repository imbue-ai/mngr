import io
import json

import click
import pytest

from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_wait.cli import _emit_state_change
from imbue.mngr_wait.cli import _output_result
from imbue.mngr_wait.cli import _read_target_from_stdin
from imbue.mngr_wait.data_types import CombinedState
from imbue.mngr_wait.data_types import StateChange
from imbue.mngr_wait.data_types import WaitResult
from imbue.mngr_wait.data_types import WaitTarget
from imbue.mngr_wait.primitives import WaitTargetType


def _make_matched_result() -> WaitResult:
    return WaitResult(
        target=WaitTarget(identifier="test-agent", target_type=WaitTargetType.AGENT),
        is_matched=True,
        is_timed_out=False,
        final_state=CombinedState(
            host_state=HostState.RUNNING,
            agent_state=AgentLifecycleState.DONE,
        ),
        matched_state="DONE",
        elapsed_seconds=5.0,
        state_changes=(),
    )


def _make_timed_out_result() -> WaitResult:
    return WaitResult(
        target=WaitTarget(identifier="test-agent", target_type=WaitTargetType.AGENT),
        is_matched=False,
        is_timed_out=True,
        final_state=CombinedState(
            host_state=HostState.RUNNING,
            agent_state=AgentLifecycleState.RUNNING,
        ),
        matched_state=None,
        elapsed_seconds=30.0,
        state_changes=(),
    )


def test_emit_state_change_human_format(capsys: pytest.CaptureFixture[str]) -> None:
    change = StateChange(
        field="agent_state",
        old_value="RUNNING",
        new_value="WAITING",
        elapsed_seconds=5.0,
    )
    _emit_state_change(change, OutputFormat.HUMAN)
    out = capsys.readouterr().out
    assert "agent_state changed: RUNNING -> WAITING" in out
    assert "5.0s" in out


def test_emit_state_change_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    change = StateChange(
        field="host_state",
        old_value="RUNNING",
        new_value="STOPPED",
        elapsed_seconds=10.0,
    )
    _emit_state_change(change, OutputFormat.JSONL)
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert parsed["event"] == "state_change"
    assert parsed["field"] == "host_state"
    assert parsed["old_value"] == "RUNNING"
    assert parsed["new_value"] == "STOPPED"


def test_emit_state_change_json_format_produces_no_output(capsys: pytest.CaptureFixture[str]) -> None:
    change = StateChange(
        field="agent_state",
        old_value="RUNNING",
        new_value="DONE",
        elapsed_seconds=3.0,
    )
    _emit_state_change(change, OutputFormat.JSON)
    out = capsys.readouterr().out
    assert out == ""


def test_output_result_matched_human_shows_target_and_state(capsys: pytest.CaptureFixture[str]) -> None:
    result = _make_matched_result()
    output_opts = OutputOptions(
        output_format=OutputFormat.HUMAN,
        format_template=None,
        is_quiet=False,
    )
    _output_result(result, output_opts)
    out = capsys.readouterr().out
    assert "test-agent" in out
    assert "DONE" in out
    assert "5.0s" in out


def test_output_result_timed_out_human_shows_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    result = _make_timed_out_result()
    output_opts = OutputOptions(
        output_format=OutputFormat.HUMAN,
        format_template=None,
        is_quiet=False,
    )
    _output_result(result, output_opts)
    out = capsys.readouterr().out
    assert "Timed out" in out
    assert "test-agent" in out


def test_output_result_json_format_emits_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    result = _make_matched_result()
    output_opts = OutputOptions(
        output_format=OutputFormat.JSON,
        format_template=None,
        is_quiet=False,
    )
    _output_result(result, output_opts)
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert parsed["target"] == "test-agent"
    assert parsed["is_matched"] is True
    assert parsed["matched_state"] == "DONE"
    assert parsed["final_host_state"] == "RUNNING"
    assert parsed["final_agent_state"] == "DONE"


def test_output_result_jsonl_format_emits_event(capsys: pytest.CaptureFixture[str]) -> None:
    result = _make_matched_result()
    output_opts = OutputOptions(
        output_format=OutputFormat.JSONL,
        format_template=None,
        is_quiet=False,
    )
    _output_result(result, output_opts)
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert parsed["event"] == "result"
    assert parsed["target"] == "test-agent"
    assert parsed["is_matched"] is True


def test_output_result_unmatched_not_timed_out(capsys: pytest.CaptureFixture[str]) -> None:
    result = WaitResult(
        target=WaitTarget(identifier="test-host", target_type=WaitTargetType.HOST),
        is_matched=False,
        is_timed_out=False,
        final_state=CombinedState(host_state=HostState.RUNNING),
        matched_state=None,
        elapsed_seconds=2.0,
        state_changes=(),
    )
    output_opts = OutputOptions(
        output_format=OutputFormat.HUMAN,
        format_template=None,
        is_quiet=False,
    )
    _output_result(result, output_opts)
    out = capsys.readouterr().out
    assert "without match" in out
    assert "test-host" in out


# === _read_target_from_stdin ===


def test_read_target_from_stdin_reads_line() -> None:
    stdin = io.StringIO("agent-abc123\n")
    result = _read_target_from_stdin(stdin=stdin)
    assert result == "agent-abc123"


def test_read_target_from_stdin_strips_whitespace() -> None:
    stdin = io.StringIO("  host-xyz789  \n")
    result = _read_target_from_stdin(stdin=stdin)
    assert result == "host-xyz789"


def test_read_target_from_stdin_raises_on_empty() -> None:
    stdin = io.StringIO("\n")
    with pytest.raises(click.UsageError, match="No target provided"):
        _read_target_from_stdin(stdin=stdin)


def test_read_target_from_stdin_raises_on_eof() -> None:
    stdin = io.StringIO("")
    with pytest.raises(click.UsageError, match="No target provided"):
        _read_target_from_stdin(stdin=stdin)


# === _output_result with state_changes ===


def test_output_result_json_includes_state_changes(capsys: pytest.CaptureFixture[str]) -> None:
    result = WaitResult(
        target=WaitTarget(identifier="test-agent", target_type=WaitTargetType.AGENT),
        is_matched=True,
        is_timed_out=False,
        final_state=CombinedState(
            host_state=HostState.STOPPED,
            agent_state=AgentLifecycleState.DONE,
        ),
        matched_state="DONE",
        elapsed_seconds=10.0,
        state_changes=(
            StateChange(
                field="agent_state",
                old_value="RUNNING",
                new_value="DONE",
                elapsed_seconds=10.0,
            ),
        ),
    )
    output_opts = OutputOptions(
        output_format=OutputFormat.JSON,
        format_template=None,
        is_quiet=False,
    )
    _output_result(result, output_opts)
    out = capsys.readouterr().out
    parsed = json.loads(out.strip())
    assert len(parsed["state_changes"]) == 1
    assert parsed["state_changes"][0]["field"] == "agent_state"
    assert parsed["state_changes"][0]["old_value"] == "RUNNING"
    assert parsed["state_changes"][0]["new_value"] == "DONE"
