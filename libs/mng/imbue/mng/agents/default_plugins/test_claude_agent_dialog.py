from pathlib import Path

import pytest

from imbue.mng.agents.default_plugins.claude_agent import DialogDetectedError
from imbue.mng.agents.default_plugins.claude_agent_test import make_claude_agent
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import SendMessageError
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.utils.polling import wait_for
from imbue.mng.utils.testing import cleanup_tmux_session


@pytest.mark.acceptance
@pytest.mark.tmux
def test_send_message_raises_dialog_detected_when_dialog_visible(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """send_message should raise DialogDetectedError when a dialog is blocking the pane."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    session_name = agent.session_name

    try:
        agent.host.execute_command(
            f"tmux new-session -d -s '{session_name}' 'echo \"Do you want to proceed?\"; sleep 847601'",
            timeout_seconds=5.0,
        )

        wait_for(
            lambda: agent._check_pane_contains(session_name, "Do you want to proceed?"),
            timeout=5.0,
            error_message="Dialog text not visible in pane",
        )

        with pytest.raises(DialogDetectedError, match="permission dialog"):
            agent.send_message("hello")
    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.acceptance
@pytest.mark.tmux
def test_send_message_does_not_raise_dialog_detected_when_no_dialog(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mng_ctx: MngContext
) -> None:
    """send_message should not raise DialogDetectedError when no dialog is present.

    The send will fail for other reasons (no real Claude Code process), but
    the important thing is that it gets past the dialog check.
    """
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mng_ctx)
    session_name = agent.session_name

    try:
        agent.host.execute_command(
            f"tmux new-session -d -s '{session_name}' 'echo \"Normal output here\"; sleep 847602'",
            timeout_seconds=5.0,
        )

        wait_for(
            lambda: agent._check_pane_contains(session_name, "Normal output here"),
            timeout=5.0,
            error_message="Content not visible in pane",
        )

        # Should NOT raise DialogDetectedError. Will raise SendMessageError
        # because there's no real Claude Code process to handle the input.
        with pytest.raises(SendMessageError) as exc_info:
            agent.send_message("hello")
        assert not isinstance(exc_info.value, DialogDetectedError)
    finally:
        cleanup_tmux_session(session_name)
