from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import NOTIFICATION_HOOK_EVENT
from imbue.mngr_claude.testing import RELEASE_TEST_MODEL
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import commit_hook_payload_saver
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_saved_payloads
from imbue.mngr_claude.testing import wait_for_agent_to_stop_running_before_idle_notification
from imbue.mngr_claude.testing import wait_for_native_session_file
from imbue.mngr_claude.testing import wait_for_session_record

_UNAVAILABLE_MODEL: Final[str] = "claude-mngr-release-test-no-such-model"
_STOP_FAILURE_EVENT: Final[str] = "StopFailure"
_STOP_FAILURE_CONSUMER: Final[str] = (
    "claude_config's StopFailure hook, which runs wait_for_stop_hook.sh to mark the agent inactive "
    "(its queued-prompt check cuts the queue off at the isApiErrorMessage record)"
)


class _UnavailableModelClaudeReleaseProfile(ClaudeReleaseProfile):
    """A claude release profile whose agent asks the API for a model it does not have."""

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        return [_UNAVAILABLE_MODEL if arg == RELEASE_TEST_MODEL else arg for arg in super().create_extra_args(ctx)]


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_turn_that_ends_on_an_api_error_leaves_the_agent_waiting(tmp_path: Path) -> None:
    """A turn that dies on an API error ends with the agent reporting WAITING, not RUNNING until claude's idle notice.

    Pins that claude routes such a turn end to the StopFailure hook event and records the error as an
    assistant record with ``isApiErrorMessage: true`` and a timestamp.
    """
    profile = _UnavailableModelClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    dump_dir = tmp_path / "stop-failure-payloads"
    commit_hook_payload_saver(ctx.project_dir, dump_dir, (_STOP_FAILURE_EVENT, NOTIFICATION_HOOK_EVENT), None)
    agent_name = f"claude-stopfailure-{get_short_random_string()}"
    drift = drift_note(ctx, "the API-error turn end")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")

        wait_for_session_record(
            wait_for_native_session_file(ctx),
            lambda record: record.get("type") == "assistant"
            and record.get("isApiErrorMessage") is True
            and isinstance(record.get("timestamp"), str),
            failure=f"a turn asking for the unavailable model {_UNAVAILABLE_MODEL!r} wrote no assistant record with "
            f"isApiErrorMessage true and a timestamp {drift}; consumers: {_STOP_FAILURE_CONSUMER}, "
            "mngr_claude_subagent_proxy.subagent_wait.is_api_error_event",
        )
        assert poll_until(
            lambda: any(p.get("hook_event_name") == _STOP_FAILURE_EVENT for p in read_saved_payloads(dump_dir)),
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            poll_interval=1.0,
        ), (
            f"the API-error turn end raised no {_STOP_FAILURE_EVENT} hook event {drift}; payloads seen: "
            f"{[p.get('hook_event_name') for p in read_saved_payloads(dump_dir)]}; consumer: {_STOP_FAILURE_CONSUMER}"
        )

        wait_for_agent_to_stop_running_before_idle_notification(
            profile,
            ctx,
            dump_dir,
            after="a turn ended on an API error",
            drift=drift,
            consumer=_STOP_FAILURE_CONSUMER,
        )
