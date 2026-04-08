import json
import subprocess
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.ratchet_testing.ratchets import assert_posix_compatible
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_claude.headless_claude_agent import HeadlessClaude
from imbue.mngr_claude.headless_claude_agent import HeadlessClaudeAgentConfig
from imbue.mngr_claude.headless_claude_agent import extract_text_delta


def _make_headless_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: HeadlessClaudeAgentConfig | AgentTypeConfig | None = None,
    agent_cls: type[HeadlessClaude] = HeadlessClaude,
) -> tuple[HeadlessClaude, Host]:
    """Create a HeadlessClaude (or subclass) agent with a real local host for testing."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    if agent_config is None:
        agent_config = HeadlessClaudeAgentConfig(check_installation=False)

    mngr_ctx = local_provider.mngr_ctx
    agent = agent_cls.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-headless"),
        agent_type=AgentTypeName("headless_claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=agent_config,
        host=host,
    )
    return agent, host


def _patch_agent_as_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch HeadlessClaude.get_lifecycle_state to return STOPPED so stream_output terminates."""
    monkeypatch.setattr(HeadlessClaude, "get_lifecycle_state", lambda self: AgentLifecycleState.STOPPED)


def _write_fake_agent_output(
    host: Host,
    agent: HeadlessClaude,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Write synthetic stdout.jsonl and stderr.log to simulate claude output.

    Creates the agent state directory (normally set up by the agent lifecycle)
    and writes the provided content to the output files.
    """
    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stdout.jsonl").write_text(stdout)
    (agent_dir / "stderr.log").write_text(stderr)


class _AlwaysFinishedHeadlessClaude(HeadlessClaude):
    """HeadlessClaude subclass with fast timeouts that always reports as finished.

    Used to test _wait_for_stdout_file's two-phase grace period logic.
    Overrides _is_agent_finished to always return True (simulating the race
    condition where tmux reports the agent as DONE during startup) and uses
    short timeouts to keep tests fast.
    """

    _startup_grace_seconds: float = 0.3
    _stdout_poll_timeout: float = 1.5

    def _is_agent_finished(self) -> bool:
        return True


class _CreatesFileOnSecondPollAgent(_AlwaysFinishedHeadlessClaude):
    """Simulates a file appearing mid-poll during the grace period.

    On the first _file_exists_on_host call for the stdout path, returns
    False (file doesn't exist yet). On the second call, creates the file
    then returns the real result. This proves the poller checked at least
    once without finding the file, then found it on a subsequent check.
    """

    _stdout_poll_count: int = 0

    def _file_exists_on_host(self, path: Path) -> bool:
        if path == self._get_stdout_path():
            self._stdout_poll_count += 1
            if self._stdout_poll_count >= 2:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
        return super()._file_exists_on_host(path)


# =============================================================================
# Tests for HeadlessClaude overrides
# =============================================================================


def test_preflight_send_message_raises(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """_preflight_send_message should raise SendMessageError for headless agents."""
    agent, _host = _make_headless_agent(local_provider, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent._preflight_send_message("some-target")


def test_send_message_raises(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """send_message should raise SendMessageError because _preflight blocks it."""
    agent, _host = _make_headless_agent(local_provider, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent.send_message("hello")


def test_uses_paste_detection_send_returns_false(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    agent, _host = _make_headless_agent(local_provider, tmp_path)
    assert agent.uses_paste_detection_send() is False


def test_get_tui_ready_indicator_returns_none(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    agent, _host = _make_headless_agent(local_provider, tmp_path)
    assert agent.get_tui_ready_indicator() is None


# =============================================================================
# Tests for assemble_command
# =============================================================================


def test_assemble_command_includes_print_and_redirect(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """assemble_command should include --print and redirect stdout and stderr."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert "--print" in cmd
    assert "$MNGR_AGENT_STATE_DIR/stdout.jsonl" in cmd
    assert ">" in cmd
    assert "$MNGR_AGENT_STATE_DIR/stderr.log" in cmd
    assert "2>" in cmd


def test_assemble_command_includes_agent_args(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """assemble_command should pass through agent args."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    cmd = agent.assemble_command(
        host,
        agent_args=("--system-prompt", "test", "--output-format", "stream-json"),
        command_override=None,
    )
    assert "--system-prompt" in cmd
    assert "test" in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd


def test_assemble_command_no_session_resumption(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """assemble_command should NOT include session resumption logic."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert "--resume" not in cmd
    assert "--session-id" not in cmd
    assert "MAIN_CLAUDE_SESSION_ID" not in cmd


def test_assemble_command_no_background_tasks(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """assemble_command should NOT include background task scripts."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert "claude_background_tasks.sh" not in cmd


def test_assemble_command_uses_command_override(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """assemble_command should use command_override when provided."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    cmd = agent.assemble_command(host, agent_args=(), command_override=CommandString("/custom/claude"))
    assert cmd.startswith("/custom/claude --print")


def test_assemble_command_raises_without_command(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """assemble_command should raise NoCommandDefinedError when no command is set."""
    # Use base AgentTypeConfig which has command=None by default
    config = AgentTypeConfig()
    agent, host = _make_headless_agent(local_provider, tmp_path, agent_config=config)
    with pytest.raises(NoCommandDefinedError):
        agent.assemble_command(host, agent_args=(), command_override=None)


def test_assemble_command_is_posix_compatible(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """Assembled commands are sent via tmux send-keys to the user's shell, which may not be bash."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    command = agent.assemble_command(host, agent_args=(), command_override=None)

    assert_posix_compatible(str(command))


# =============================================================================
# Tests for stream_output
# =============================================================================


def _make_stream_json_line(text: str) -> str:
    """Build a stream-json line for a text_delta event."""
    return json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        }
    )


def test_stream_output_yields_text_deltas(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should parse stream-json and yield text chunks."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    lines = [
        _make_stream_json_line("Hello "),
        _make_stream_json_line("world!"),
        json.dumps({"type": "result", "is_error": False}),
    ]
    _write_fake_agent_output(host, agent, stdout="\n".join(lines) + "\n")

    chunks = list(agent.stream_output())

    assert chunks == ["Hello ", "world!"]


def test_stream_output_raises_when_empty_file(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should raise MngrError when stdout file exists but is empty.

    Creates both stdout.jsonl (empty) and stderr.log (empty) so the error
    fallback chain stops before reaching tmux pane capture.
    """
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(host, agent)

    with pytest.raises(MngrError, match="no details available"):
        list(agent.stream_output())


def test_stream_output_handles_file_without_trailing_newline(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should not drop a partial line at the end of the file when there is no trailing newline."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(host, agent, stdout=_make_stream_json_line("no trailing newline"))

    chunks = list(agent.stream_output())

    assert chunks == ["no trailing newline"]


def test_stream_output_raises_with_stream_json_error_result(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should surface error from stream-json result event with is_error=true."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(
        host,
        agent,
        stdout=(
            '{"type":"system","subtype":"init","session_id":"abc"}\n'
            '{"type":"result","subtype":"success","is_error":true,"result":"err-7f3a9b2e"}\n'
        ),
    )

    with pytest.raises(MngrError, match="err-7f3a9b2e"):
        list(agent.stream_output())


def test_stream_output_raises_error_result_even_after_yielding_text(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should raise MngrError for is_error result even if text was yielded first."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(
        host,
        agent,
        stdout=(
            _make_stream_json_line("partial output") + "\n"
            '{"type":"result","subtype":"success","is_error":true,"result":"err-c4d8e1f0"}\n'
        ),
    )

    with pytest.raises(MngrError, match="err-c4d8e1f0"):
        list(agent.stream_output())


def test_stream_output_combines_result_error_and_stderr_after_partial_output(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should combine result error and stderr even after yielding text."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(
        host,
        agent,
        stdout=(
            _make_stream_json_line("partial") + "\n"
            '{"type":"result","subtype":"success","is_error":true,"result":"err-a1b2c3d4"}\n'
        ),
        stderr="stderr-e5f6g7h8\n",
    )

    with pytest.raises(MngrError, match="err-a1b2c3d4") as exc_info:
        list(agent.stream_output())
    assert "stderr-e5f6g7h8" in str(exc_info.value)


def test_stream_output_combines_stderr_and_stdout_errors(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should include both stderr and stdout errors when both are present."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(
        host,
        agent,
        stdout='{"type":"result","subtype":"success","is_error":true,"result":"err-d9e0f1a2"}\n',
        stderr="stderr-b3c4d5e6\n",
    )

    with pytest.raises(MngrError, match="err-d9e0f1a2") as exc_info:
        list(agent.stream_output())
    assert "stderr-b3c4d5e6" in str(exc_info.value)


def test_stream_output_raises_with_stderr_content(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should surface stderr.log content in the error when stdout is empty."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(host, agent, stderr="stderr-f7g8h9i0\n")

    with pytest.raises(MngrError, match="stderr-f7g8h9i0"):
        list(agent.stream_output())


@pytest.mark.tmux
def test_stream_output_falls_back_to_pane_capture(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should fall back to pane capture when no redirect files exist.

    Creates a real tmux session with error text visible in the pane, then
    verifies the fallback chain reaches pane capture and surfaces that text.
    """
    _patch_agent_as_stopped(monkeypatch)
    agent, _host = _make_headless_agent(local_provider, tmp_path)
    # Use a short grace period so the test doesn't wait 10s before
    # checking lifecycle state (the default grace period exceeds the
    # pytest timeout).
    agent._startup_grace_seconds = 0.5
    session = agent.session_name

    # Start a session that immediately prints error text and exits.
    # Using a command argument to new-session ensures the text is in the
    # pane buffer without needing send-keys + sleep.
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "200",
            "-y",
            "50",
            "echo pane-err-j1k2l3m4; exec cat",
        ],
        check=True,
    )
    try:
        with pytest.raises(MngrError, match="pane-err-j1k2l3m4"):
            list(agent.stream_output())
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)


# =============================================================================
# Tests for _wait_for_stdout_file grace period
# =============================================================================


def test_grace_period_ignores_lifecycle_state(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """During the grace period, _wait_for_stdout_file should ignore lifecycle state.

    Even though _is_agent_finished returns True immediately, phase 1 should
    keep polling for the file. We verify this by wrapping _file_exists_on_host
    to create the file on the second poll -- proving the poller checked at
    least once without finding it, then found it on a subsequent check.
    """
    agent, _host = _make_headless_agent(local_provider, tmp_path, agent_cls=_CreatesFileOnSecondPollAgent)
    stdout_path = agent._get_stdout_path()

    result = agent._wait_for_stdout_file(stdout_path)

    assert result is True


def test_phase2_trusts_lifecycle_after_grace_period(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """After the grace period, _wait_for_stdout_file should trust lifecycle state.

    When the agent reports finished and the file never appears, phase 2
    should detect the agent is done and return False.
    """
    agent, _host = _make_headless_agent(local_provider, tmp_path, agent_cls=_AlwaysFinishedHeadlessClaude)
    stdout_path = agent._get_stdout_path()

    # Do NOT create the stdout file -- agent is "finished" and file never appeared
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    result = agent._wait_for_stdout_file(stdout_path)
    elapsed = time.monotonic() - start

    assert result is False
    # Should have waited at least the grace period before trusting lifecycle state
    assert elapsed >= agent._startup_grace_seconds * 0.9


def test_file_during_grace_period_returns_true_immediately(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """If the stdout file already exists, _wait_for_stdout_file should return True immediately."""
    agent, _host = _make_headless_agent(local_provider, tmp_path, agent_cls=_AlwaysFinishedHeadlessClaude)
    stdout_path = agent._get_stdout_path()

    # Pre-create the file
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("")

    start = time.monotonic()
    result = agent._wait_for_stdout_file(stdout_path)
    elapsed = time.monotonic() - start

    assert result is True
    # Should return almost immediately, well before the grace period expires
    assert elapsed < agent._startup_grace_seconds * 0.5


# =============================================================================
# Tests for registration
# =============================================================================


def test_headless_claude_registered(
    local_provider: LocalProviderInstance,
) -> None:
    """headless_claude should be registered as an agent type."""
    types = list_registered_agent_types()
    assert "headless_claude" in types


# =============================================================================
# Tests for extract_text_delta
# =============================================================================


def test_extract_text_delta_valid_event() -> None:
    """A valid content_block_delta event should return the text."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    )
    assert extract_text_delta(event) == "hello"


def test_extract_text_delta_non_delta_event() -> None:
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_start", "index": 0},
        }
    )
    assert extract_text_delta(event) is None


def test_extract_text_delta_malformed_json() -> None:
    assert extract_text_delta("not valid json {{{") is None


def test_extract_text_delta_non_stream_event() -> None:
    event = json.dumps({"type": "result", "subtype": "success"})
    assert extract_text_delta(event) is None


def test_extract_text_delta_missing_delta() -> None:
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0},
        }
    )
    assert extract_text_delta(event) is None


def test_extract_text_delta_event_not_dict() -> None:
    event = json.dumps({"type": "stream_event", "event": "not_a_dict"})
    assert extract_text_delta(event) is None


def test_extract_text_delta_non_text_delta_type() -> None:
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        }
    )
    assert extract_text_delta(event) is None


def test_extract_text_delta_delta_not_dict() -> None:
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": "not_a_dict",
            },
        }
    )
    assert extract_text_delta(event) is None


def test_extract_text_delta_text_not_string() -> None:
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": 42},
            },
        }
    )
    assert extract_text_delta(event) is None
