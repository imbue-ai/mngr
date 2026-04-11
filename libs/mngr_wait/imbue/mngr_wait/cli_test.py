import io
import json
from pathlib import Path

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_wait.cli import _emit_state_change
from imbue.mngr_wait.cli import _output_result
from imbue.mngr_wait.cli import _read_target_from_stdin
from imbue.mngr_wait.cli import _validate_event_type
from imbue.mngr_wait.cli import wait
from imbue.mngr_wait.data_types import CombinedState
from imbue.mngr_wait.data_types import StateChange
from imbue.mngr_wait.data_types import WaitResult
from imbue.mngr_wait.data_types import WaitTarget
from imbue.mngr_wait.primitives import WaitTargetType
from imbue.mngr_wait.testing import write_lifecycle_event


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


# === _validate_event_type ===


def test_validate_event_type_accepts_agent_ready() -> None:
    assert _validate_event_type("AGENT_READY") == "AGENT_READY"


def test_validate_event_type_accepts_agent_starting() -> None:
    assert _validate_event_type("AGENT_STARTING") == "AGENT_STARTING"


def test_validate_event_type_is_case_insensitive() -> None:
    assert _validate_event_type("agent_ready") == "AGENT_READY"
    assert _validate_event_type("Agent_Starting") == "AGENT_STARTING"


def test_validate_event_type_rejects_invalid_event() -> None:
    with pytest.raises(click.UsageError, match="Invalid event type"):
        _validate_event_type("INVALID_EVENT")


def test_validate_event_type_rejects_lifecycle_state_as_event() -> None:
    with pytest.raises(click.UsageError, match="Invalid event type"):
        _validate_event_type("RUNNING")


# === CLI integration tests ===


def _create_local_agent_dir(host_dir: Path, agent_id: AgentId) -> None:
    """Create the agent directory structure needed for local provider to discover the agent."""
    agent_dir = host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Write minimal agent data.json
    data = {
        "id": str(agent_id),
        "name": "test-agent",
        "type": "claude",
        "work_dir": "/tmp/test",
        "command": "echo test",
        "create_time": "2026-01-01T00:00:00Z",
        "start_on_boot": False,
        "labels": {},
    }
    (agent_dir / "data.json").write_text(json.dumps(data))


@pytest.mark.tmux
def test_wait_cli_event_flag_returns_success_when_event_exists(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that `mngr wait <agent> --event AGENT_READY` returns 0 when the event exists."""
    agent_id = AgentId.generate()
    _create_local_agent_dir(temp_host_dir, agent_id)
    write_lifecycle_event(temp_host_dir, agent_id, "AGENT_READY")

    result = cli_runner.invoke(wait, [str(agent_id), "--event", "AGENT_READY", "--interval", "1s"], obj=plugin_manager)
    assert result.exit_code == 0


@pytest.mark.tmux
def test_wait_cli_event_flag_times_out_when_event_missing(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that `mngr wait <agent> --event AGENT_READY --timeout 1s` returns 2 on timeout."""
    agent_id = AgentId.generate()
    _create_local_agent_dir(temp_host_dir, agent_id)

    result = cli_runner.invoke(
        wait,
        [str(agent_id), "--event", "AGENT_READY", "--timeout", "1s", "--interval", "1s"],
        obj=plugin_manager,
    )
    assert result.exit_code == 2


@pytest.mark.tmux
def test_wait_cli_event_and_state_are_mutually_exclusive(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that combining --event with state args produces an error."""
    agent_id = AgentId.generate()
    _create_local_agent_dir(temp_host_dir, agent_id)

    result = cli_runner.invoke(
        wait,
        [str(agent_id), "DONE", "--event", "AGENT_READY", "--timeout", "1s"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot combine --event with state arguments" in result.output


@pytest.mark.tmux
def test_wait_cli_state_returns_success_when_already_matched(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that `mngr wait <agent> STOPPED` returns 0 when agent is already stopped."""
    agent_id = AgentId.generate()
    _create_local_agent_dir(temp_host_dir, agent_id)

    result = cli_runner.invoke(
        wait,
        [str(agent_id), "STOPPED", "--interval", "1s"],
        obj=plugin_manager,
    )
    # Agent is stopped (no tmux session), so should match immediately
    assert result.exit_code == 0
