import json
import shlex
import subprocess
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.ratchet_testing.ratchets import assert_posix_compatible
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.agents.base_headless_agent import BaseHeadlessAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import CreateAgentOptions
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
from imbue.mngr_claude.headless_claude_agent import extract_assistant_message_id
from imbue.mngr_claude.headless_claude_agent import extract_assistant_text
from imbue.mngr_claude.headless_claude_agent import extract_message_start_id
from imbue.mngr_claude.headless_claude_agent import extract_text_delta
from imbue.mngr_claude.plugin import ClaudeAgent

# =============================================================================
# MRO invariant test
# =============================================================================


def test_headless_claude_resolves_all_shared_method_conflicts() -> None:
    """Ensure HeadlessClaude explicitly resolves any method defined on both ClaudeAgent and BaseHeadlessAgent.

    HeadlessClaude has diamond inheritance: it extends both
    NoPermissionsClaudeAgent (-> ClaudeAgent -> BaseAgent) and
    BaseHeadlessAgent (-> BaseAgent). When both ClaudeAgent and
    BaseHeadlessAgent define the same method, the MRO silently picks
    ClaudeAgent's version (which appears first). HeadlessClaude must
    explicitly override any such method to select the correct behavior.
    """

    def _callable_method_names(cls: type) -> set[str]:
        return {
            name
            for name, val in cls.__dict__.items()
            if not name.startswith("__") and (callable(val) or isinstance(val, (classmethod, staticmethod)))
        }

    base_headless_methods = _callable_method_names(BaseHeadlessAgent)
    claude_methods = _callable_method_names(ClaudeAgent)
    headless_claude_methods = _callable_method_names(HeadlessClaude)

    # Methods defined on both sides of the diamond
    shared = base_headless_methods & claude_methods

    # HeadlessClaude must explicitly override every shared method
    unresolved = shared - headless_claude_methods
    assert not unresolved, (
        f"BaseHeadlessAgent and ClaudeAgent both define these methods, but HeadlessClaude "
        f"does not explicitly override them: {unresolved}. Without an explicit override on "
        f"HeadlessClaude, the MRO silently picks ClaudeAgent's version. Add overrides to "
        f"HeadlessClaude that delegate to the correct base class."
    )


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


def test_stage_initial_message_writes_prompt_file_in_state_dir(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """stage_initial_message writes the prompt into the agent's state dir (not work dir).

    Writing to the state dir means the file is blown away when the agent is
    destroyed, so it never leaks into an in-place source directory.
    """
    agent, host = _make_headless_agent(local_provider, tmp_path)
    # The agent state dir is normally created by create_agent_state; mirror
    # that here so stage_initial_message's write has a parent directory.
    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    agent.stage_initial_message("please fix the tests")

    assert (agent_dir / ".mngr-prompt").read_text() == "please fix the tests"
    # Work dir remains clean.
    assert not (agent.work_dir / ".mngr-prompt").exists()


def test_assemble_command_appends_prompt_ref_when_initial_message_set(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When initial_message is passed (via Host.create_agent_state from
    CreateAgentOptions.initial_message) and agent_args do not already
    reference the prompt file, assemble_command appends a cat of
    $MNGR_AGENT_STATE_DIR/.mngr-prompt so claude actually receives the
    user's message.
    """
    agent, host = _make_headless_agent(local_provider, tmp_path)

    cmd = agent.assemble_command(host, agent_args=(), command_override=None, initial_message="hi there")

    assert ".mngr-prompt" in cmd
    # Cat-form against the state dir (not the work dir), so cleanup is automatic.
    assert 'cat "$MNGR_AGENT_STATE_DIR/.mngr-prompt"' in cmd


def test_assemble_command_does_not_duplicate_prompt_ref(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """If the caller already included a state-dir .mngr-prompt reference in agent_args,
    assemble_command must not append a second one (would double-feed the prompt).
    """
    agent, host = _make_headless_agent(local_provider, tmp_path)

    explicit_prompt_arg = '"$(cat "$MNGR_AGENT_STATE_DIR/.mngr-prompt")"'
    cmd = agent.assemble_command(
        host, agent_args=(explicit_prompt_arg,), command_override=None, initial_message="hi there"
    )

    assert cmd.count(".mngr-prompt") == 1


def test_assemble_command_no_prompt_ref_when_no_initial_message(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """No initial_message supplied = no appended prompt reference."""
    agent, host = _make_headless_agent(local_provider, tmp_path)

    cmd = agent.assemble_command(host, agent_args=(), command_override=None)

    assert ".mngr-prompt" not in cmd


def test_create_agent_state_persists_prompt_cat_in_command_for_headless_claude(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """End-to-end check: Host.create_agent_state must thread
    options.initial_message into HeadlessClaude.assemble_command so the
    persisted command (in data.json) actually cats the staged prompt.

    Unit tests on assemble_command alone cannot catch a case where
    create_agent_state forgets to forward initial_message. Reading the
    command back from data.json mirrors what start_agents does in
    production via _get_agent_command.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    options = CreateAgentOptions(
        name=AgentName("test-prompt-plumbing"),
        agent_type=AgentTypeName("headless_claude"),
        command=CommandString("claude"),
        initial_message="hello from the regression test",
    )
    agent = host.create_agent_state(work_dir, options)

    data_path = host.host_dir / "agents" / str(agent.id) / "data.json"
    persisted = json.loads(data_path.read_text())
    assert 'cat "$MNGR_AGENT_STATE_DIR/.mngr-prompt"' in persisted["command"]


def test_assemble_command_is_posix_compatible(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """Assembled commands are sent via tmux send-keys to the user's shell, which may not be bash."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    command = agent.assemble_command(host, agent_args=(), command_override=None)

    assert_posix_compatible(str(command))


def test_assemble_command_quotes_agent_args_with_shell_metacharacters(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """Agent args containing shell metacharacters must survive shell parsing as single tokens."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    prompt = "respond with only the word HELLO; echo pwned & rm -rf $HOME"
    cmd = agent.assemble_command(
        host,
        agent_args=("--verbose", prompt),
        command_override=None,
    )

    cmd_before_redirect = str(cmd).split(" >", 1)[0]
    tokens = shlex.split(cmd_before_redirect)
    assert prompt in tokens, f"prompt should be a single token after shell parsing, got tokens={tokens!r}"


def test_assemble_command_does_not_double_quote_pre_quoted_cli_args(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """cli_args reach assemble_command already shell-safe (see split_cli_args_string).

    Re-quoting them would break the documented round-trip contract: a string config like
    --settings '{"key": "value"}' is shlex-split in non-POSIX mode so the JSON keeps its
    outer single quotes in-token. A second round of shlex.quote would wrap those single
    quotes in another layer, breaking the final shell command.
    """
    # Same tokens split_cli_args_string produces for --settings '{"key": "value"}'
    pre_quoted_json = '\'{"key": "value"}\''
    config = HeadlessClaudeAgentConfig(
        cli_args=("--settings", pre_quoted_json),
        check_installation=False,
    )
    agent, host = _make_headless_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)

    cmd_before_redirect = str(cmd).split(" >", 1)[0]
    tokens = shlex.split(cmd_before_redirect)
    assert '{"key": "value"}' in tokens, (
        f'pre-quoted cli_arg JSON should pass through shell as \'{{"key": "value"}}\', got tokens={tokens!r}'
    )


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


def _make_assistant_message_line(text: str, message_id: str | None = None) -> str:
    """Build a stream-json line for a top-level `assistant` event with one text block.

    This is the envelope `claude --output-format stream-json` emits by default
    (without `--include-partial-messages`) on v2.1.114 and similar versions.
    """
    message: dict[str, object] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }
    if message_id is not None:
        message["id"] = message_id
    return json.dumps({"type": "assistant", "message": message})


def _make_message_start_line(message_id: str) -> str:
    """Build a stream-json `message_start` line carrying a message id.

    With `--include-partial-messages`, claude emits this before the per-token
    text deltas. The id lets the parser correlate the deltas with a later
    top-level `assistant` summary that carries the same id.
    """
    return json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {"id": message_id, "role": "assistant"},
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


@pytest.mark.tmux
def test_stream_output_raises_when_empty_file(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should raise MngrError when stdout file exists but is empty.

    Creates both stdout.jsonl (empty) and stderr.log (empty). The error
    fallback chain reaches pane capture (which returns None -- no tmux
    session for the test agent), then falls through to "no details available".
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


def test_stream_output_handles_non_string_result_error_payload(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should fall back to 'unknown error' when `result` is non-string.

    Defensive: claude's API contract says `result` is a string for error
    results, but if a future version emits null / a number / a structured
    object, we honor the declared str | None return type rather than
    leaking a non-string into the error path.
    """
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    _write_fake_agent_output(
        host,
        agent,
        stdout='{"type":"result","subtype":"success","is_error":true,"result":null}\n',
    )

    with pytest.raises(MngrError, match="unknown error"):
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


# =============================================================================
# Tests for extract_assistant_text and dual-envelope stream_output
# =============================================================================


def test_extract_assistant_text_single_text_block() -> None:
    event = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        }
    )
    assert extract_assistant_text(event) == "hello"


def test_extract_assistant_text_concatenates_multiple_text_blocks() -> None:
    event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world!"},
                ],
            },
        }
    )
    assert extract_assistant_text(event) == "Hello world!"


def test_extract_assistant_text_skips_non_text_blocks() -> None:
    event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}},
                    {"type": "text", "text": "after tool"},
                ],
            },
        }
    )
    assert extract_assistant_text(event) == "after tool"


def test_extract_assistant_text_returns_none_when_no_text_blocks() -> None:
    event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}],
            },
        }
    )
    assert extract_assistant_text(event) is None


def test_extract_assistant_text_returns_none_for_non_assistant_event() -> None:
    event = json.dumps({"type": "system", "subtype": "init"})
    assert extract_assistant_text(event) is None


def test_extract_assistant_text_returns_none_for_malformed_json() -> None:
    assert extract_assistant_text("not valid json {{{") is None


def test_extract_assistant_text_returns_none_when_message_not_dict() -> None:
    event = json.dumps({"type": "assistant", "message": "not_a_dict"})
    assert extract_assistant_text(event) is None


def test_extract_assistant_text_returns_none_when_content_not_list() -> None:
    event = json.dumps({"type": "assistant", "message": {"content": "not_a_list"}})
    assert extract_assistant_text(event) is None


# A tool_use-only assistant event for the "tool_use_only_assistant_event_ends_turn"
# scenario. `is_definitely_different_message` is False (same id), so without a
# state reset the next assistant text would be diffed against the stale buffer
# "Hello" rather than treated as a fresh turn.
_TOOL_USE_ONLY_ASSISTANT_EVENT = json.dumps(
    {
        "type": "assistant",
        "message": {
            "id": "msg_a",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}],
        },
    }
)

# Each entry is a pytest.param wrapping (lines, expected_chunks) and tagged
# with a behavioral id. The docstring above the parametrize decorator covers
# the contract these scenarios collectively describe (the dual-envelope
# stream_output parser).
_DUAL_ENVELOPE_SCENARIOS = [
    pytest.param(
        # Without --include-partial-messages, claude emits only
        # system/assistant/result events (no `stream_event` deltas, as on
        # claude CLI v2.1.114+). The parser must still surface the response.
        [
            '{"type":"system","subtype":"init","session_id":"abc"}',
            _make_assistant_message_line("Hello from claude"),
            '{"type":"result","subtype":"success","is_error":false,"result":"Hello from claude"}',
        ],
        ["Hello from claude"],
        id="yields_assistant_envelope_without_partial_messages",
    ),
    pytest.param(
        # With --include-partial-messages, claude emits per-token stream_event
        # deltas AND a final `assistant` summary containing the same text. The
        # parser must yield each token once (from the deltas) and skip the
        # summary so text is not double-emitted.
        [
            _make_stream_json_line("Hello "),
            _make_stream_json_line("world!"),
            _make_assistant_message_line("Hello world!"),
            '{"type":"result","subtype":"success","is_error":false,"result":"Hello world!"}',
        ],
        ["Hello ", "world!"],
        id="does_not_double_emit_when_partial_and_assistant_interleave",
    ),
    pytest.param(
        # A subsequent assistant turn after a fully-streamed turn must still be
        # yielded -- the dedup state resets at each `assistant` boundary. A
        # tool-using session (partial deltas + assistant summary, then a second
        # assistant-only turn) otherwise loses the second turn's text.
        [
            _make_stream_json_line("first "),
            _make_stream_json_line("answer"),
            _make_assistant_message_line("first answer"),
            _make_assistant_message_line("second answer"),
            '{"type":"result","subtype":"success","is_error":false,"result":"second answer"}',
        ],
        ["first ", "answer", "second answer"],
        id="yields_assistant_envelopes_across_multiple_turns",
    ),
    pytest.param(
        # When the assistant summary contains text beyond what the deltas
        # yielded, that trailing text is emitted instead of silently dropped.
        # Models the failure mode where partial deltas end early (dropped
        # frame, truncated stream, etc.) and only the final summary carries
        # the full message text.
        [
            _make_message_start_line("msg_a"),
            _make_stream_json_line("Hello "),
            _make_stream_json_line("world"),
            _make_assistant_message_line("Hello world! And then some.", message_id="msg_a"),
            '{"type":"result","subtype":"success","is_error":false,"result":"Hello world! And then some."}',
        ],
        ["Hello ", "world", "! And then some."],
        id="emits_trailing_text_when_summary_extends_past_deltas",
    ),
    pytest.param(
        # If the partial-stream message_start id does not match the assistant
        # summary id, treat them as separate messages: the deltas are already
        # yielded and the full summary is yielded too, with no dedup attempt.
        # Models a streamed message whose summary was dropped before a new
        # message's summary arrived.
        [
            _make_message_start_line("msg_a"),
            _make_stream_json_line("first message body"),
            _make_assistant_message_line("second message body", message_id="msg_b"),
            '{"type":"result","subtype":"success","is_error":false,"result":"second message body"}',
        ],
        ["first message body", "second message body"],
        id="yields_full_summary_when_assistant_id_does_not_match_streaming_id",
    ),
    pytest.param(
        # A new `message_start` clears per-turn state so the second turn's
        # summary diff is computed against the second turn's deltas only.
        [
            _make_message_start_line("msg_a"),
            _make_stream_json_line("first"),
            _make_assistant_message_line("first", message_id="msg_a"),
            _make_message_start_line("msg_b"),
            _make_stream_json_line("second"),
            _make_assistant_message_line("second extra", message_id="msg_b"),
            '{"type":"result","subtype":"success","is_error":false,"result":"second extra"}',
        ],
        ["first", "second", " extra"],
        id="message_start_resets_buffer_across_turns",
    ),
    pytest.param(
        # If deltas drift from the summary and no id info is available, fall
        # back to yielding the full summary rather than silently dropping it.
        # A possible partial double-emit is preferred over losing the
        # assistant message entirely.
        [
            _make_stream_json_line("drifted prefix"),
            _make_assistant_message_line("totally different summary text"),
            '{"type":"result","subtype":"success","is_error":false,"result":"totally different summary text"}',
        ],
        ["drifted prefix", "totally different summary text"],
        id="yields_full_summary_when_buffer_is_not_a_prefix",
    ),
    pytest.param(
        # A tool_use-only assistant event must end the current turn's dedup
        # state. Otherwise stale `yielded_text_chunks` from the streamed
        # deltas would dedup the next assistant text event whose id we
        # cannot disambiguate -- causing the second message's text to be
        # partially suppressed when it shares a prefix with the first
        # message's already-yielded text. Here the streamed "Hello" delta is
        # yielded as-is; the tool_use-only event ends the turn with no text
        # emit AND clears the per-turn buffer; the second assistant event's
        # full text is then yielded.
        [
            _make_message_start_line("msg_a"),
            _make_stream_json_line("Hello"),
            _TOOL_USE_ONLY_ASSISTANT_EVENT,
            _make_assistant_message_line("Hello there"),
            '{"type":"result","subtype":"success","is_error":false,"result":"Hello there"}',
        ],
        ["Hello", "Hello there"],
        id="tool_use_only_assistant_event_ends_turn",
    ),
]


@pytest.mark.parametrize(("lines", "expected_chunks"), _DUAL_ENVELOPE_SCENARIOS)
def test_stream_output_dual_envelope_dispatch(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str],
    expected_chunks: list[str],
) -> None:
    """stream_output correctly dispatches between the partial-stream `stream_event`
    deltas and the top-level `assistant` summary envelope.

    Each parametrized case covers one behavioral contract of the dual-envelope
    parser. See _DUAL_ENVELOPE_SCENARIOS above for the per-case rationale.
    """
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)
    _write_fake_agent_output(host, agent, stdout="\n".join(lines) + "\n")

    chunks = list(agent.stream_output())

    assert chunks == expected_chunks


def test_extract_assistant_message_id_returns_id_when_present() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "msg_xyz", "role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        }
    )
    assert extract_assistant_message_id(line) == "msg_xyz"


def test_extract_assistant_message_id_returns_none_when_id_missing() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        }
    )
    assert extract_assistant_message_id(line) is None


def test_extract_assistant_message_id_returns_none_for_non_assistant_event() -> None:
    line = json.dumps({"type": "system", "subtype": "init"})
    assert extract_assistant_message_id(line) is None


def test_extract_assistant_message_id_returns_none_for_malformed_json() -> None:
    assert extract_assistant_message_id("not valid json {{{") is None


def test_extract_assistant_message_id_returns_none_when_message_not_dict() -> None:
    line = json.dumps({"type": "assistant", "message": "not_a_dict"})
    assert extract_assistant_message_id(line) is None


def test_extract_assistant_message_id_returns_none_when_id_not_string() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"id": 42, "role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        }
    )
    assert extract_assistant_message_id(line) is None


def test_extract_message_start_id_returns_id_for_message_start_event() -> None:
    line = _make_message_start_line("msg_abc")
    assert extract_message_start_id(line) == "msg_abc"


def test_extract_message_start_id_returns_none_for_text_delta_event() -> None:
    assert extract_message_start_id(_make_stream_json_line("hello")) is None


def test_extract_message_start_id_returns_none_for_top_level_assistant_event() -> None:
    line = _make_assistant_message_line("hi", message_id="msg_abc")
    assert extract_message_start_id(line) is None


def test_extract_message_start_id_returns_none_for_malformed_json() -> None:
    assert extract_message_start_id("not valid json {{{") is None


def test_extract_message_start_id_returns_none_when_event_not_dict() -> None:
    line = json.dumps({"type": "stream_event", "event": "not_a_dict"})
    assert extract_message_start_id(line) is None


def test_extract_message_start_id_returns_none_when_message_not_dict() -> None:
    line = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": "not_a_dict"},
        }
    )
    assert extract_message_start_id(line) is None


def test_extract_message_start_id_returns_none_when_id_not_string() -> None:
    line = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": 42, "role": "assistant"}},
        }
    )
    assert extract_message_start_id(line) is None
