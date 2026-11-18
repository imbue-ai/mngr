"""How an Antigravity agent puts a message into its pane, on a real tmux server."""

import shlex
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.mock_host_test import CommandCountingHost
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr_antigravity.plugin import AntigravityAgent
from imbue.mngr_antigravity.plugin import AntigravityAgentConfig


@pytest.mark.tmux
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    ("message", "is_pasted"),
    [("?what changed 36284", True), ("?", True), ("what changed? 36284", False)],
    ids=["leading-question-mark", "lone-question-mark", "question-mark-later"],
)
def test_message_starting_with_question_mark_is_pasted_rather_than_typed(
    local_provider: LocalProviderInstance, tmp_path: Path, message: str, is_pasted: bool
) -> None:
    """A message starting with ``?`` is pasted, since agy takes a typed ``?`` as its help key; others are typed."""
    host = make_local_host_of_class(local_provider, CommandCountingHost)
    agent = AntigravityAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName(f"question-mark-{AgentId.generate().get_uuid().hex[:8]}"),
        agent_type=AgentTypeName("antigravity"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=AntigravityAgentConfig(check_installation=False),
        host=host,
    )
    window_name = local_provider.mngr_ctx.config.tmux.primary_window_name
    host.execute_idempotent_command(
        f"tmux new-session -d -s {shlex.quote(agent.session_name)} -n {shlex.quote(window_name)} -x 200 -y 24 cat",
        timeout_seconds=5.0,
    )
    try:
        agent._send_tmux_literal_keys(agent.tmux_target, message)

        wait_for(
            lambda: message in (agent.capture_pane_content() or ""),
            timeout=10.0,
            error_message=f"Expected the pane to show {message!r}",
        )
        assert any("paste-buffer -p" in command for command in host.executed_commands) == is_pasted
    finally:
        cleanup_tmux_session(agent.session_name)
