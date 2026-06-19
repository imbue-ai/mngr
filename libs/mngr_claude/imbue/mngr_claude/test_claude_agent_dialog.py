from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr_claude.plugin import DialogDetectedError
from imbue.mngr_claude.plugin_test import make_claude_agent

# The fake tmux sessions these tests drive just need to stay alive long enough
# for the assertions to run; the exact duration is irrelevant. A large value
# keeps the session from exiting mid-test (the ``finally`` blocks kill it).
_KEEP_ALIVE_SLEEP_SECONDS = 847601


@pytest.mark.acceptance
@pytest.mark.tmux
def test_send_message_raises_dialog_detected_when_dialog_visible(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """send_message should raise DialogDetectedError when a dialog is blocking the pane."""
    agent, _ = make_claude_agent(local_provider, tmp_path, temp_mngr_ctx)
    session_name = agent.session_name
    window_name = agent.mngr_ctx.config.tmux.primary_window_name

    try:
        # Name the primary window so it matches agent.tmux_target (which targets by name).
        agent.host.execute_idempotent_command(
            f"tmux new-session -d -s '{session_name}' -n '{window_name}' 'echo \"Yes, I trust this folder\"; sleep {_KEEP_ALIVE_SLEEP_SECONDS}'",
            timeout_seconds=5.0,
        )

        wait_for(
            lambda: agent._check_pane_contains(agent.tmux_target, "Yes, I trust this folder"),
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
    window_name = agent.mngr_ctx.config.tmux.primary_window_name

    try:
        # The pane must contain the TUI-ready indicator (Claude's input-prompt
        # glyph), because send_message now waits for readiness before pasting.
        # A bare pane without it would (correctly) block on that wait; here we
        # are exercising the no-dialog path, so the pane stands in for a ready
        # Claude TUI.
        ready_glyph = agent.get_tui_ready_indicator()
        # Name the primary window so it matches agent.tmux_target (which targets by name).
        agent.host.execute_idempotent_command(
            f"tmux new-session -d -s '{session_name}' -n '{window_name}' 'echo \"{ready_glyph} Normal output here\"; sleep {_KEEP_ALIVE_SLEEP_SECONDS}'",
            timeout_seconds=5.0,
        )

        wait_for(
            lambda: agent._check_pane_contains(agent.tmux_target, "Normal output here"),
            timeout=5.0,
            error_message="Content not visible in pane",
        )

        # The dialog preflight must clear (no dialog is present), after which
        # send_message proceeds to the paste/submit phase. With no real Claude
        # process, that phase deterministically times out -- either
        # wait_for_paste_visible ("Timeout waiting for pasted content to
        # appear") or the per-session submit hook in _send_enter_and_validate
        # ("Timeout waiting for message submission signal"). Both raise the
        # base SendMessageError with a "Timeout waiting for" reason, which a
        # DialogDetectedError ("A dialog is blocking the agent's input ...")
        # never contains. Matching that substring therefore proves the failure
        # came from the downstream send path, not the dialog gate or some
        # unrelated SendMessageError. We do NOT positively assert that "hello"
        # reached the pane: the keep-alive session runs a bare `sleep` with no
        # input handler, so whether the typed text echoes back is not reliable
        # enough to assert on here.
        with pytest.raises(SendMessageError, match="Timeout waiting for") as exc_info:
            agent.send_message("hello")
        assert not isinstance(exc_info.value, DialogDetectedError)
    finally:
        cleanup_tmux_session(session_name)
