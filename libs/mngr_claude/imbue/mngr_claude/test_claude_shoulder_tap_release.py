from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.claude_config import KEYBINDINGS_FILENAME
from imbue.mngr_claude.claude_config import get_agent_claude_config_dir
from imbue.mngr_claude.claude_config import is_tap_binding_active
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import records_after_user_prompt
from imbue.mngr_claude.testing import send_keys_to_agent_pane
from imbue.mngr_claude.testing import session_message_text
from imbue.mngr_claude.testing import start_held_tool_call
from imbue.mngr_claude.testing import wait_for_native_session_file

# The keystroke the default workspace template's shoulder tap sends.
_TAP_KEYS: Final[str] = "M-q"
_FLUSH_TIMEOUT_SECONDS: Final[float] = 45.0
_TAP_CONSUMER: Final[str] = (
    "claude_config.ensure_chat_cancel_tap_keybinding (meta+q -> chat:cancel in the Chat context) and "
    "is_tap_binding_active, which the shoulder tap gates on"
)


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_shoulder_tap_flushes_a_queued_message_into_the_session(tmp_path: Path) -> None:
    """The shoulder tap's meta+q cancels the live turn, so a message queued behind a long tool call is answered at once."""
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    tool_marker = tmp_path / "held-tool-running"
    agent_name = f"claude-tap-{get_short_random_string()}"
    queued_prompt = f"Reply with just the word FLUSHED-{get_short_random_string()}."
    drift = drift_note(ctx, "claude's handling of the tap keybinding")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        start_held_tool_call(profile, ctx, agent_name, tool_marker)
        send_expecting_success(profile, ctx, agent_name, queued_prompt)
        session_file = wait_for_native_session_file(ctx)

        def queued_prompt_has_reply() -> bool:
            return any(
                record.get("type") == "assistant" and session_message_text(record).strip() != ""
                for record in records_after_user_prompt(read_session_records(session_file), queued_prompt)
            )

        state_dir = agent_state_dir(ctx.host_dir)
        keybindings_path = get_agent_claude_config_dir(state_dir) / KEYBINDINGS_FILENAME
        assert is_tap_binding_active(keybindings_path, state_dir / "claude_process_started"), (
            f"mngr does not report the shoulder tap active for a freshly created agent ({keybindings_path}); "
            f"consumer: {_TAP_CONSUMER}"
        )

        send_keys_to_agent_pane(ctx, agent_name, _TAP_KEYS, is_literal=False)
        assert poll_until(queued_prompt_has_reply, timeout=_FLUSH_TIMEOUT_SECONDS, poll_interval=1.0), (
            f"{_FLUSH_TIMEOUT_SECONDS:.0f}s after the tap, the message queued behind a held "
            "tool call has no reply, so meta+q did not cancel the turn and flush the queue "
            f"although mngr reports the tap active {drift}; consumer: {_TAP_CONSUMER}"
        )
