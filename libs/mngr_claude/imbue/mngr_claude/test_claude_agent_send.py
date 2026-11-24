"""A whole send to a stand-in Claude Code TUI on a real tmux server."""

import json
import shlex
import shutil
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest
from pydantic import Field

from imbue.mngr.agents.mock_host_test import CommandCountingHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.tmux import AGENT_PANE_ID_OPTION
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.dialogs import has_input_prompt_line
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin_test import make_claude_agent

_FAKE_CLAUDE_TUI_SCRIPT_PATH: Final[Path] = (
    Path(__file__).parent / "resources" / "test_fixtures" / "fake_claude_tui.py"
)

_FAKE_CLAUDE_TUI_HOLD_FLAG: Final[str] = "--hold"

_EXPECTED_HOST_COMMANDS_PER_SEND: Final[int] = 6

_requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; required by the Claude submission-evidence probes"
)


class _FakeTuiClaudeAgent(ClaudeAgent):
    """A Claude agent whose process is the fake Claude TUI, known by the command name tmux reports for its pane."""

    fake_tui_process_name: str = Field(default="", description="The command name tmux reports for the fake TUI")

    def get_expected_process_name(self) -> str:
        return self.fake_tui_process_name


@contextmanager
def _claude_agent_on_fake_tui(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    tui_flags: tuple[str, ...] = (),
) -> Generator[tuple[_FakeTuiClaudeAgent, CommandCountingHost, Path], None, None]:
    """Run the fake Claude TUI with ``tui_flags`` as a Claude agent's pane; yield the agent, its host and transcript."""
    host = make_local_host_of_class(local_provider, CommandCountingHost)
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, host=host, agent_class=_FakeTuiClaudeAgent)
    assert isinstance(agent, _FakeTuiClaudeAgent)
    state_dir = agent._get_agent_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "claude-config"
    host.get_agent_env_path(agent).write_text(f"MNGR_AGENT_STATE_DIR={state_dir}\nCLAUDE_CONFIG_DIR={config_dir}\n")
    (state_dir / "claude_session_id").write_text("session-36284\n")
    transcript_path = (
        config_dir / "projects" / encode_claude_project_dir_name(agent.work_dir.resolve()) / "session-36284.jsonl"
    )

    session_arg = shlex.quote(f"={agent.session_name}:")
    window_name = temp_mngr_ctx.config.tmux.primary_window_name
    window_arg = shlex.quote(f"={agent.session_name}:{window_name}")
    pane_command = shlex.join([sys.executable, str(_FAKE_CLAUDE_TUI_SCRIPT_PATH), str(transcript_path), *tui_flags])
    host.execute_idempotent_command(
        f"tmux new-session -d -s {shlex.quote(agent.session_name)} -n {shlex.quote(window_name)} "
        f"-x 200 -y 30 {shlex.quote(pane_command)} && "
        f"tmux set-option -t {session_arg} {AGENT_PANE_ID_OPTION} "
        f"\"$(tmux display-message -p -t {window_arg} '#{{pane_id}}')\"",
        timeout_seconds=5.0,
    )
    try:
        wait_for(lambda: has_input_prompt_line(agent.capture_pane_content() or ""), timeout=10.0)
        agent.fake_tui_process_name = host.execute_idempotent_command(
            f"tmux display-message -p -t {window_arg} '#{{pane_current_command}}'", timeout_seconds=5.0
        ).stdout.strip()
        yield agent, host, transcript_path
    finally:
        host.execute_idempotent_command(
            f"tmux kill-session -t {shlex.quote(f'={agent.session_name}')}", timeout_seconds=5.0
        )


def _read_last_transcript_message(transcript_path: Path) -> str:
    return json.loads(transcript_path.read_text().splitlines()[-1])["message"]["content"]


@pytest.mark.tmux
@pytest.mark.timeout(60)
@_requires_jq
def test_send_delivers_to_claude_tui_within_its_host_command_budget(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A send reaches Claude's transcript, confirmed, with one process spawn per step that needs one.

    Every host command is a process spawn, and under gVisor each costs tens of milliseconds, so
    the count is the send's latency budget: a step that starts resolving the pane or capturing
    the screen again shows up here.
    """
    with _claude_agent_on_fake_tui(local_provider, tmp_path, temp_mngr_ctx) as (agent, host, transcript_path):
        host.executed_commands.clear()

        agent.send_message("please run the full test suite 36284")

        assert _read_last_transcript_message(transcript_path) == "please run the full test suite 36284"
        assert len(host.executed_commands) == _EXPECTED_HOST_COMMANDS_PER_SEND, host.executed_commands


@pytest.mark.tmux
@pytest.mark.timeout(60)
@_requires_jq
def test_send_of_lone_question_mark_reaches_claude_transcript(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A lone ``?`` is delivered as a message, not taken by Claude as its shortcuts-panel key."""
    with _claude_agent_on_fake_tui(local_provider, tmp_path, temp_mngr_ctx) as (agent, _, transcript_path):
        agent.send_message("?")

        assert _read_last_transcript_message(transcript_path) == "?"


@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_send_claude_takes_without_starting_a_turn_returns_with_the_agent_reading_running(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A message Claude takes while its SessionStart hooks hold back the turn leaves the agent RUNNING, not idle."""
    with _claude_agent_on_fake_tui(
        local_provider, tmp_path, temp_mngr_ctx, tui_flags=(_FAKE_CLAUDE_TUI_HOLD_FLAG,)
    ) as (agent, _, transcript_path):
        assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING

        agent.send_message("please run the full test suite 58213")

        assert not transcript_path.exists()
        assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING


@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_send_the_screen_shows_taken_only_after_the_window_returns_with_the_agent_reading_running(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A held message no probe can recognize, settled as taken once the window ends, leaves the agent RUNNING."""
    with _claude_agent_on_fake_tui(
        local_provider, tmp_path, temp_mngr_ctx, tui_flags=(_FAKE_CLAUDE_TUI_HOLD_FLAG,)
    ) as (agent, _, transcript_path):
        agent.confirmation_timeout_seconds = 1.0
        assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING

        agent.send_message("请运行全部测试")

        message_events_path = agent._get_agent_dir() / "events" / "messages" / "events.jsonl"
        assert json.loads(message_events_path.read_text().splitlines()[-1])["type"] == "submission_confirmed_by_screen"
        assert not transcript_path.exists()
        assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING
