from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr_claude.plugin import DialogDetectedError
from imbue.mngr_claude.plugin_test import make_claude_agent


@pytest.mark.acceptance
@pytest.mark.tmux
def test_send_message_raises_dialog_detected_when_dialog_visible(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """send_message should raise DialogDetectedError when a dialog is blocking the pane."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    session_name = agent.session_name

    try:
        agent.host.execute_command(
            f"tmux new-session -d -s '{session_name}' 'echo \"Yes, I trust this folder\"; sleep 847601'",
            timeout_seconds=5.0,
        )

        wait_for(
            lambda: agent._check_pane_contains(session_name, "Yes, I trust this folder"),
            timeout=5.0,
            error_message="Dialog text not visible in pane",
        )

        with pytest.raises(DialogDetectedError, match="trust dialog"):
            agent.send_message("hello")
    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.acceptance
@pytest.mark.tmux
def test_send_message_does_not_raise_dialog_detected_when_no_dialog(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """send_message should not raise DialogDetectedError when no dialog is present.

    The send will fail for other reasons (no real Claude Code process), but
    the important thing is that it gets past the dialog check.
    """
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    # Use a short submission timeout so the test does not block for 60s waiting
    # for a tmux wait-for signal that will never arrive (no real Claude process)
    agent.enter_submission_timeout_seconds = 1.0
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
