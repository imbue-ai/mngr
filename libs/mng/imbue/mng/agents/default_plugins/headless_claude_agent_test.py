import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.agents.agent_registry import list_registered_agent_types
from imbue.mng.agents.default_plugins.headless_claude_agent import HeadlessClaude
from imbue.mng.agents.default_plugins.headless_claude_agent import HeadlessClaudeAgentConfig
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.errors import NoCommandDefinedError
from imbue.mng.errors import SendMessageError
from imbue.mng.hosts.host import Host
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.instance import LocalProviderInstance


def _make_headless_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: HeadlessClaudeAgentConfig | AgentTypeConfig | None = None,
) -> tuple[HeadlessClaude, Host]:
    """Create a HeadlessClaude agent with a real local host for testing."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    if agent_config is None:
        agent_config = HeadlessClaudeAgentConfig(check_installation=False)

    mng_ctx = local_provider.mng_ctx
    agent = HeadlessClaude.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-headless"),
        agent_type=AgentTypeName("headless_claude"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=mng_ctx,
        agent_config=agent_config,
        host=host,
    )
    return agent, host


def _patch_agent_as_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch HeadlessClaude.get_lifecycle_state to return STOPPED so stream_output terminates."""
    monkeypatch.setattr(HeadlessClaude, "get_lifecycle_state", lambda self: AgentLifecycleState.STOPPED)


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
    """assemble_command should include --print and redirect stdout to stdout.jsonl."""
    agent, host = _make_headless_agent(local_provider, tmp_path)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert "--print" in cmd
    assert "$MNG_AGENT_STATE_DIR/stdout.jsonl" in cmd
    assert ">" in cmd


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

    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = agent_dir / "stdout.jsonl"
    lines = [
        _make_stream_json_line("Hello "),
        _make_stream_json_line("world!"),
        json.dumps({"type": "result", "is_error": False}),
    ]
    stdout_path.write_text("\n".join(lines) + "\n")

    chunks = list(agent.stream_output())

    assert chunks == ["Hello ", "world!"]


def test_stream_output_handles_empty_file(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should handle an empty output file gracefully."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = agent_dir / "stdout.jsonl"
    stdout_path.write_text("")

    chunks = list(agent.stream_output())

    assert chunks == []


def test_stream_output_handles_file_without_trailing_newline(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should not drop a partial line at the end of the file when there is no trailing newline."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_agent(local_provider, tmp_path)

    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = agent_dir / "stdout.jsonl"

    content = _make_stream_json_line("no trailing newline")
    stdout_path.write_text(content)

    chunks = list(agent.stream_output())

    assert chunks == ["no trailing newline"]


def test_stream_output_returns_when_agent_stopped_and_no_file(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_output should return cleanly when agent is stopped and no file exists."""
    _patch_agent_as_stopped(monkeypatch)
    agent, _host = _make_headless_agent(local_provider, tmp_path)

    chunks = list(agent.stream_output())

    assert chunks == []


# =============================================================================
# Tests for registration
# =============================================================================


def test_headless_claude_registered(
    local_provider: LocalProviderInstance,
) -> None:
    """headless_claude should be registered as an agent type."""
    types = list_registered_agent_types()
    assert "headless_claude" in types
