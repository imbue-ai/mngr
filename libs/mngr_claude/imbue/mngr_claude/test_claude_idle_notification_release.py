from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import marker_path
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import IDLE_PROMPT_NOTIFICATION_TYPE
from imbue.mngr_claude.testing import NOTIFICATION_HOOK_EVENT
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import commit_hook_payload_saver
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_saved_payloads
from imbue.mngr_claude.testing import wait_for_agent_to_stop_running
from imbue.mngr_claude.testing import wait_for_turn_to_end

# Claude notifies after about a minute without input; this bounds that and the hook's run.
_IDLE_NOTIFICATION_TIMEOUT_SECONDS: Final[float] = 180.0
_TURN_PROMPT: Final[str] = "Reply with just OK."
_IDLE_PROMPT_CONSUMER: Final[str] = (
    "claude_config's Notification hook (matcher idle_prompt), which clears the active marker"
)


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_idle_notification_clears_a_stranded_running_marker(tmp_path: Path) -> None:
    """Claude's idle notification brings an agent whose turn-end signal was lost back to WAITING.

    Pins that claude raises an ``idle_prompt`` notification for an idle agent; the test strands the
    active marker itself, as a lost turn end does.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    dump_dir = tmp_path / "notification-payloads"
    commit_hook_payload_saver(ctx.project_dir, dump_dir, (NOTIFICATION_HOOK_EVENT,), "*")
    agent_name = f"claude-idlenotice-{get_short_random_string()}"
    drift = drift_note(ctx, "the idle notification")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, _TURN_PROMPT)
        wait_for_turn_to_end(profile, ctx, _TURN_PROMPT, drift)

        marker_path(ctx.host_dir).touch()

        assert poll_until(
            lambda: any(
                p.get("notification_type") == IDLE_PROMPT_NOTIFICATION_TYPE for p in read_saved_payloads(dump_dir)
            ),
            timeout=_IDLE_NOTIFICATION_TIMEOUT_SECONDS,
            poll_interval=2.0,
        ), (
            f"claude raised no {NOTIFICATION_HOOK_EVENT} hook event with notification_type "
            f"{IDLE_PROMPT_NOTIFICATION_TYPE!r} for an idle agent {drift}; notification types seen: "
            f"{[p.get('notification_type') for p in read_saved_payloads(dump_dir)]}; "
            f"consumer: {_IDLE_PROMPT_CONSUMER}"
        )
        wait_for_agent_to_stop_running(
            profile,
            ctx,
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            failure=f"claude's {IDLE_PROMPT_NOTIFICATION_TYPE} notification fired but the agent still reports RUNNING "
            f"{drift}; consumer: {_IDLE_PROMPT_CONSUMER}",
        )
