from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import marker_path
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.testing import AGENT_CREATE_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import NOTIFICATION_HOOK_EVENT
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import commit_hook_payload_saver
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_saved_payloads
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import session_message_text
from imbue.mngr_claude.testing import start_held_tool_call
from imbue.mngr_claude.testing import wait_for_agent_to_stop_running_before_idle_notification
from imbue.mngr_claude.testing import wait_for_native_session_file

_SESSION_START_EVENT: Final[str] = "SessionStart"
_RESUME_SOURCE: Final[str] = "resume"
_LIFECYCLE_TIMEOUT_SECONDS: Final[float] = 300.0
_SESSION_START_CONSUMER: Final[str] = (
    "claude_config's SessionStart hook, which clears the active marker for a startup or resume source"
)
_INTERRUPT_SENTINEL_PREFIX: Final[str] = "[Request interrupted by user"


def _is_interrupt_sentinel(record: Mapping[str, Any]) -> bool:
    return record.get("type") == "user" and session_message_text(record).lstrip().startswith(
        _INTERRUPT_SENTINEL_PREFIX
    )


def _saved_session_start_sources(dump_dir: Path) -> Counter[str]:
    return Counter(
        str(payload.get("source"))
        for payload in read_saved_payloads(dump_dir)
        if payload.get("hook_event_name") == _SESSION_START_EVENT
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_resumed_after_a_stop_mid_turn_reports_waiting(tmp_path: Path) -> None:
    """An agent stopped mid-turn reports WAITING once started again, not RUNNING until claude's idle notice.

    Pins that claude reports a ``resume`` SessionStart source for a session mngr starts again.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    dump_dir = tmp_path / "session-start-payloads"
    commit_hook_payload_saver(ctx.project_dir, dump_dir, (_SESSION_START_EVENT, NOTIFICATION_HOOK_EVENT), None)
    tool_marker = tmp_path / "held-tool-running"
    agent_name = f"claude-resume-{get_short_random_string()}"
    drift = drift_note(ctx, "the SessionStart source")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        start_held_tool_call(profile, ctx, agent_name, tool_marker)
        stop = profile.run_mngr(ctx, "stop", agent_name, timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        assert stop.returncode == 0, f"stop failed:\n{stop.stdout}\n{stop.stderr}"
        active_marker = marker_path(ctx.host_dir)
        assert active_marker.exists(), "stopping mid-turn did not leave the active marker behind, so nothing is tested"
        sources_before_start = _saved_session_start_sources(dump_dir)

        start = profile.run_mngr(ctx, "start", agent_name, timeout=AGENT_CREATE_TIMEOUT_SECONDS)
        assert start.returncode == 0, f"start failed:\n{start.stdout}\n{start.stderr}"
        assert poll_until(
            lambda: _saved_session_start_sources(dump_dir).total() > sources_before_start.total(),
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            poll_interval=1.0,
        ), (
            f"the restarted claude raised no {_SESSION_START_EVENT} hook event {drift}; consumer: {_SESSION_START_CONSUMER}"
        )
        restart_sources = _saved_session_start_sources(dump_dir) - sources_before_start
        assert _RESUME_SOURCE in restart_sources, (
            f"the restarted claude's {_SESSION_START_EVENT} sources are {sorted(restart_sources.elements())}, none "
            f"{_RESUME_SOURCE!r} {drift}; consumer: {_SESSION_START_CONSUMER}"
        )

        wait_for_agent_to_stop_running_before_idle_notification(
            profile,
            ctx,
            dump_dir,
            after="restarting an agent stopped mid-turn",
            drift=drift,
            consumer=_SESSION_START_CONSUMER,
        )
        last_record = read_session_records(wait_for_native_session_file(ctx))[-1]
        assert not _is_interrupt_sentinel(last_record), (
            "the resumed session ends on claude's interrupt sentinel, so stream_transcript.sh's interrupt heal could "
            "have cleared RUNNING instead of the SessionStart hook; nothing is tested "
            f"{drift_note(ctx, 'how claude ends a session resumed after a stop mid-turn')}"
        )
