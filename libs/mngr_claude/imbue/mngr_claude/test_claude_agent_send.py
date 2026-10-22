"""A whole send to a stand-in Claude Code TUI on a real tmux server."""

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.mock_host_test import CommandCountingHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.tmux import AGENT_PANE_ID_OPTION
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.dialogs import has_input_prompt_line
from imbue.mngr_claude.plugin_test import make_claude_agent

_FAKE_CLAUDE_TUI_SCRIPT_PATH: Final[Path] = (
    Path(__file__).parent / "resources" / "test_fixtures" / "fake_claude_tui.py"
)

_EXPECTED_HOST_COMMANDS_PER_SEND: Final[int] = 6


@pytest.mark.tmux
@pytest.mark.timeout(60)
@pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; required by the Claude submission-evidence probes"
)
def test_send_delivers_to_claude_tui_within_its_host_command_budget(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A send reaches Claude's transcript, confirmed, with one process spawn per step that needs one.

    Every host command is a process spawn, and under gVisor each costs tens of milliseconds, so
    the count is the send's latency budget: a step that starts resolving the pane or capturing
    the screen again shows up here.
    """
    host = make_local_host_of_class(local_provider, CommandCountingHost)
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx, host=host)
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
    pane_command = shlex.join([sys.executable, str(_FAKE_CLAUDE_TUI_SCRIPT_PATH), str(transcript_path)])
    host.execute_idempotent_command(
        f"tmux new-session -d -s {shlex.quote(agent.session_name)} -n {shlex.quote(window_name)} "
        f"-x 200 -y 30 {shlex.quote(pane_command)} && "
        f"tmux set-option -t {session_arg} {AGENT_PANE_ID_OPTION} "
        f"\"$(tmux display-message -p -t {shlex.quote(f'={agent.session_name}:{window_name}')} '#{{pane_id}}')\"",
        timeout_seconds=5.0,
    )
    try:
        wait_for(lambda: has_input_prompt_line(agent.capture_pane_content() or ""), timeout=10.0)
        host.executed_commands.clear()

        agent.send_message("please run the full test suite 36284")

        last_record = json.loads(transcript_path.read_text().splitlines()[-1])
        assert last_record["message"]["content"] == "please run the full test suite 36284"
        assert len(host.executed_commands) == _EXPECTED_HOST_COMMANDS_PER_SEND, host.executed_commands
    finally:
        host.execute_idempotent_command(
            f"tmux kill-session -t {shlex.quote(f'={agent.session_name}')}", timeout_seconds=5.0
        )
