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
from imbue.mngr.errors import MngrError
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


def _make_headless_command_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: HeadlessCommandConfig | AgentTypeConfig | None = None,
) -> tuple[HeadlessCommand, Host]:
    """Create a HeadlessCommand agent with a real local host for testing."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    if agent_config is None:
        agent_config = HeadlessCommandConfig()

    mngr_ctx = local_provider.mngr_ctx
    agent = HeadlessCommand.model_construct(
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
    return agent, host


def _patch_agent_as_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch HeadlessCommand.get_lifecycle_state to return STOPPED so stream_output terminates."""
    monkeypatch.setattr(HeadlessCommand, "get_lifecycle_state", lambda self: AgentLifecycleState.STOPPED)


def _write_fake_agent_output(
    host: Host,
    agent: HeadlessCommand,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Write synthetic stdout.log and stderr.log to simulate command output."""
    agent_dir = host.host_dir / "agents" / str(agent.id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stdout.log").write_text(stdout)
    (agent_dir / "stderr.log").write_text(stderr)


# =============================================================================
# Tests for HeadlessCommand overrides
# =============================================================================


def test_preflight_send_message_raises(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    agent, _host = _make_headless_command_agent(local_provider, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent._preflight_send_message("some-target")


def test_send_message_raises(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    agent, _host = _make_headless_command_agent(local_provider, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent.send_message("hello")


def test_uses_paste_detection_send_returns_false(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    agent, _host = _make_headless_command_agent(local_provider, tmp_path)
    assert agent.uses_paste_detection_send() is False


def test_get_tui_ready_indicator_returns_none(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    agent, _host = _make_headless_command_agent(local_provider, tmp_path)
    assert agent.get_tui_ready_indicator() is None


# =============================================================================
# Tests for assemble_command
# =============================================================================


def test_assemble_command_redirects_stdout_and_stderr(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    config = HeadlessCommandConfig(command=CommandString("echo hello"))
    agent, host = _make_headless_command_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert '> "$MNGR_AGENT_STATE_DIR/stdout.log"' in cmd
    assert '2> "$MNGR_AGENT_STATE_DIR/stderr.log"' in cmd


def test_assemble_command_uses_config_command(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    config = HeadlessCommandConfig(command=CommandString("my-command"))
    agent, host = _make_headless_command_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert cmd.startswith("my-command")


def test_assemble_command_uses_command_override(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    config = HeadlessCommandConfig(command=CommandString("original"))
    agent, host = _make_headless_command_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=(), command_override=CommandString("/custom/cmd"))
    assert cmd.startswith("/custom/cmd")
    assert "original" not in cmd


def test_assemble_command_falls_back_to_agent_type(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """When no command is set, assemble_command uses the agent_type as a command."""
    config = HeadlessCommandConfig()
    agent, host = _make_headless_command_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert cmd.startswith("headless_command")


def test_assemble_command_includes_cli_args(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    config = HeadlessCommandConfig(command=CommandString("cmd"), cli_args=("--verbose", "--timeout=30"))
    agent, host = _make_headless_command_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert "--verbose" in cmd
    assert "--timeout=30" in cmd


def test_assemble_command_includes_agent_args(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    config = HeadlessCommandConfig(command=CommandString("cmd"))
    agent, host = _make_headless_command_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=("--arg1", "val1"), command_override=None)
    assert "--arg1" in cmd
    assert "val1" in cmd


def test_assemble_command_no_print_flag(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """assemble_command should NOT include --print (that is Claude-specific)."""
    config = HeadlessCommandConfig(command=CommandString("cmd"))
    agent, host = _make_headless_command_agent(local_provider, tmp_path, agent_config=config)
    cmd = agent.assemble_command(host, agent_args=(), command_override=None)
    assert "--print" not in cmd


# =============================================================================
# Tests for stream_output
# =============================================================================


def test_stream_output_yields_raw_text(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_command_agent(local_provider, tmp_path)
    _write_fake_agent_output(host, agent, stdout="Hello world!\nLine 2\n")

    chunks = list(agent.stream_output())

    assert "".join(chunks) == "Hello world!\nLine 2\n"


def test_stream_output_raises_when_empty_file(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_command_agent(local_provider, tmp_path)
    _write_fake_agent_output(host, agent)

    with pytest.raises(MngrError, match="no details available"):
        list(agent.stream_output())


def test_stream_output_surfaces_stderr_on_error(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stdout is empty, stderr content appears in the error."""
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_command_agent(local_provider, tmp_path)
    _write_fake_agent_output(host, agent, stderr="command not found: foobar\n")

    with pytest.raises(MngrError, match="command not found: foobar"):
        list(agent.stream_output())


@pytest.mark.tmux
def test_stream_output_falls_back_to_pane_capture(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither redirect file exists, pane capture is used as a fallback."""
    _patch_agent_as_stopped(monkeypatch)
    agent, _host = _make_headless_command_agent(local_provider, tmp_path)
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
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_as_stopped(monkeypatch)
    agent, host = _make_headless_command_agent(local_provider, tmp_path)
    _write_fake_agent_output(host, agent, stdout="chunk1chunk2")

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
