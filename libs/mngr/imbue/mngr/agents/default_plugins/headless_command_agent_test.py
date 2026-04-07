from __future__ import annotations

import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.agents.default_plugins.headless_command_agent import HeadlessCommand
from imbue.mngr.agents.default_plugins.headless_command_agent import HeadlessCommandConfig
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.providers.local.instance import LocalProviderInstance


class _AlwaysStoppedHeadlessCommand(HeadlessCommand):
    """Test subclass that always reports STOPPED lifecycle state.

    Uses inheritance to override get_lifecycle_state, ensuring
    stream_output terminates immediately when reading pre-written
    test files.
    """

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return AgentLifecycleState.STOPPED


def _make_headless_command_agent(
    host: Host,
    mngr_ctx: MngrContext,
    tmp_path: Path,
    agent_config: HeadlessCommandConfig | AgentTypeConfig | None = None,
    # Use the always-stopped subclass by default for tests that call stream_output
    is_always_stopped: bool = False,
) -> HeadlessCommand:
    """Create a HeadlessCommand agent with a real local host for testing."""
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    if agent_config is None:
        agent_config = HeadlessCommandConfig()

    cls = _AlwaysStoppedHeadlessCommand if is_always_stopped else HeadlessCommand
    return cls.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-headless-cmd"),
        agent_type=AgentTypeName("headless_command"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


def _write_fake_agent_output(
    agent: HeadlessCommand,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Write synthetic stdout.log and stderr.log to simulate command output."""
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stdout.log").write_text(stdout)
    (agent_dir / "stderr.log").write_text(stderr)


# =============================================================================
# Tests for HeadlessCommand overrides
# =============================================================================


def test_preflight_send_message_raises(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent._preflight_send_message("some-target")


def test_send_message_raises(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent.send_message("hello")


def test_uses_paste_detection_send_returns_false(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path)
    assert agent.uses_paste_detection_send() is False


def test_get_tui_ready_indicator_returns_none(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path)
    assert agent.get_tui_ready_indicator() is None


# =============================================================================
# Tests for assemble_command
# =============================================================================


def test_assemble_command_redirects_stdout_and_stderr(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    config = HeadlessCommandConfig(command=CommandString("echo hello"))
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path, agent_config=config)
    cmd = agent.assemble_command(local_host, agent_args=(), command_override=None)
    assert '> "$MNGR_AGENT_STATE_DIR/stdout.log"' in cmd
    assert '2> "$MNGR_AGENT_STATE_DIR/stderr.log"' in cmd


def test_assemble_command_no_print_flag(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """assemble_command should NOT include --print (that is Claude-specific)."""
    config = HeadlessCommandConfig(command=CommandString("cmd"))
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path, agent_config=config)
    cmd = agent.assemble_command(local_host, agent_args=(), command_override=None)
    assert "--print" not in cmd


# =============================================================================
# Tests for stream_output
# =============================================================================


def test_stream_output_yields_raw_text(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path, is_always_stopped=True)
    _write_fake_agent_output(agent, stdout="Hello world!\nLine 2\n")

    chunks = list(agent.stream_output())

    assert "".join(chunks) == "Hello world!\nLine 2\n"


def test_stream_output_raises_when_empty_file(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path, is_always_stopped=True)
    _write_fake_agent_output(agent)

    with pytest.raises(MngrError, match="no details available"):
        list(agent.stream_output())


def test_stream_output_surfaces_stderr_on_error(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When stdout is empty, stderr content appears in the error."""
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path, is_always_stopped=True)
    _write_fake_agent_output(agent, stderr="command not found: foobar\n")

    with pytest.raises(MngrError, match="command not found: foobar"):
        list(agent.stream_output())


@pytest.mark.tmux
def test_stream_output_falls_back_to_pane_capture(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When neither redirect file exists, pane capture is used as a fallback."""
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path, is_always_stopped=True)
    session = agent.session_name

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
            "echo pane-err-deadbeef; exec cat",
        ],
        check=True,
    )
    try:
        with pytest.raises(MngrError, match="pane-err-deadbeef"):
            list(agent.stream_output())
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)


def test_output_returns_joined_text(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_headless_command_agent(local_host, temp_mngr_ctx, tmp_path, is_always_stopped=True)
    _write_fake_agent_output(agent, stdout="chunk1chunk2")

    result = agent.output()

    assert result == "chunk1chunk2"


# =============================================================================
# Tests for registration
# =============================================================================


def test_headless_command_registered(
    local_provider: LocalProviderInstance,
) -> None:
    types = list_registered_agent_types()
    assert "headless_command" in types
