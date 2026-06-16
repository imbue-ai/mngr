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

    try:
        agent.host.execute_idempotent_command(
            f"tmux new-session -d -s '{session_name}' 'echo \"Yes, I trust this folder\"; sleep {_KEEP_ALIVE_SLEEP_SECONDS}'",
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
    # A real started agent has signaled input-readiness via the SessionStart
    # hook; create that marker so send_message's readiness gate passes and the
    # test exercises the dialog check + downstream send path (not the gate).
    agent._get_agent_dir().mkdir(parents=True, exist_ok=True)
    (agent._get_agent_dir() / "session_started").touch()
    session_name = agent.session_name

    try:
        agent.host.execute_idempotent_command(
            f"tmux new-session -d -s '{session_name}' 'echo \"Normal output here\"; sleep {_KEEP_ALIVE_SLEEP_SECONDS}'",
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
