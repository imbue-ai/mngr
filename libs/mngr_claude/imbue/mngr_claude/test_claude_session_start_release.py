from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_DIR_NAME
from imbue.mngr.primitives import BackgroundTask
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import read_background_task_records_in
from imbue.mngr_claude.claude_config import HOOK_BACKGROUND_TASK_NAME_PREFIX
from imbue.mngr_claude.testing import AGENT_CREATE_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import BACKGROUND_SLEEP_PROMPT
from imbue.mngr_claude.testing import BACKGROUND_SLEEP_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import NOTIFICATION_HOOK_EVENT
from imbue.mngr_claude.testing import SESSION_RECORD_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import commit_hook_payload_saver
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_saved_payloads
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import start_held_tool_call
from imbue.mngr_claude.testing import wait_for_agent_to_stop_running_before_idle_notification
from imbue.mngr_claude.testing import wait_for_turn_to_end

_SESSION_START_EVENT: Final[str] = "SessionStart"
_AFTER_RESTART_PROMPT: Final[str] = "Reply with just DONE."
_CLEARING_SOURCES: Final[frozenset[str]] = frozenset({"startup", "resume"})
_LIFECYCLE_TIMEOUT_SECONDS: Final[float] = 300.0
_SESSION_START_CONSUMER: Final[str] = (
    "claude_config's SessionStart hook, which for a startup or resume source clears the active marker and removes "
    f"the {HOOK_BACKGROUND_TASK_NAME_PREFIX}* background task records"
)
_ACTIVITY_EVENTS_RELPATH: Final[str] = "events/mngr/activity/events.jsonl"
_INTERRUPT_HEAL_REASON: Final[str] = "interrupt_heal"


def _activity_events(ctx: AgentReleaseContext) -> list[dict[str, Any]]:
    events_file = ctx.host_dir / _ACTIVITY_EVENTS_RELPATH
    return read_session_records(events_file) if events_file.exists() else []


def _saved_session_start_sources(dump_dir: Path) -> Counter[str]:
    return Counter(
        str(payload.get("source"))
        for payload in read_saved_payloads(dump_dir)
        if payload.get("hook_event_name") == _SESSION_START_EVENT
    )


def _task_records(ctx: AgentReleaseContext) -> tuple[BackgroundTask, ...]:
    return read_background_task_records_in(agent_state_dir(ctx.host_dir) / BACKGROUND_TASK_DIR_NAME)


def _start_and_wait_for_a_clearing_session_start(
    profile: ClaudeReleaseProfile,
    ctx: AgentReleaseContext,
    agent_name: str,
    dump_dir: Path,
    *,
    drift: str,
) -> None:
    sources_before_start = _saved_session_start_sources(dump_dir)
    start = profile.run_mngr(ctx, "start", agent_name, timeout=AGENT_CREATE_TIMEOUT_SECONDS)
    assert start.returncode == 0, f"start failed:\n{start.stdout}\n{start.stderr}"

    def restart_sources() -> Counter[str]:
        return _saved_session_start_sources(dump_dir) - sources_before_start

    assert poll_until(
        lambda: not _CLEARING_SOURCES.isdisjoint(restart_sources()),
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        poll_interval=1.0,
    ), (
        f"the restarted claude's {_SESSION_START_EVENT} sources are {sorted(restart_sources().elements())}, "
        f"none of {sorted(_CLEARING_SOURCES)} {drift}; consumer: {_SESSION_START_CONSUMER}"
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_resumed_after_a_stop_mid_turn_reports_waiting(tmp_path: Path) -> None:
    """An agent stopped mid-turn reports WAITING once started again, not RUNNING until claude's idle notice.

    Pins that claude reports a ``startup`` or ``resume`` SessionStart source for a session mngr starts again.
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
        assert profile.is_reporting_running(ctx.host_dir), (
            "stopping mid-turn did not leave the active marker behind, so nothing is tested"
        )
        events_before_start = len(_activity_events(ctx))
        _start_and_wait_for_a_clearing_session_start(profile, ctx, agent_name, dump_dir, drift=drift)

        wait_for_agent_to_stop_running_before_idle_notification(
            profile,
            ctx,
            dump_dir,
            after="restarting an agent stopped mid-turn",
            drift=drift,
            consumer=_SESSION_START_CONSUMER,
        )
        assert poll_until(
            lambda: len(_activity_events(ctx)) > events_before_start,
            timeout=SESSION_RECORD_TIMEOUT_SECONDS,
            poll_interval=0.2,
        ), (
            f"the restarted agent stopped reporting RUNNING but {_ACTIVITY_EVENTS_RELPATH} gained no activity event, "
            f"so what cleared the marker is unknown {drift}; consumer: {_SESSION_START_CONSUMER}"
        )
        restart_events = _activity_events(ctx)[events_before_start:]
        assert not any(event.get("reason") == _INTERRUPT_HEAL_REASON for event in restart_events), (
            "stream_transcript.sh's interrupt heal cleared RUNNING, so the SessionStart hook may not have; "
            f"nothing is tested {drift_note(ctx, 'how claude ends a session resumed after a stop mid-turn')}"
        )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_restarted_with_a_record_outstanding_comes_back_without_it(tmp_path: Path) -> None:
    """A restart clears the records the previous claude process wrote, and claude resumes none of their tasks."""
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    dump_dir = tmp_path / "session-start-payloads"
    commit_hook_payload_saver(ctx.project_dir, dump_dir, (_SESSION_START_EVENT,), None)
    agent_name = f"claude-restart-record-{get_short_random_string()}"
    turn_drift = drift_note(ctx, "the background_tasks entries of claude's Stop payload")
    drift = drift_note(ctx, "the SessionStart source")
    resume_drift = drift_note(ctx, "what claude does with background tasks across a restart")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, BACKGROUND_SLEEP_PROMPT)
        wait_for_turn_to_end(profile, ctx, BACKGROUND_SLEEP_PROMPT, turn_drift)
        assert _task_records(ctx), (
            f"the turn that backgrounded `sleep {BACKGROUND_SLEEP_SECONDS}` left no record to clear, so nothing is "
            f"tested {turn_drift}"
        )

        stop = profile.run_mngr(ctx, "stop", agent_name, timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        assert stop.returncode == 0, f"stop failed:\n{stop.stdout}\n{stop.stderr}"
        assert _task_records(ctx), (
            "stopping the agent removed its records itself, so the restart's clearing is not tested"
        )

        _start_and_wait_for_a_clearing_session_start(profile, ctx, agent_name, dump_dir, drift=drift)
        assert poll_until(
            lambda: _task_records(ctx) == (), timeout=SESSION_RECORD_TIMEOUT_SECONDS, poll_interval=1.0
        ), (
            f"the restarted claude's {_SESSION_START_EVENT} left the records {_task_records(ctx)} in place; "
            f"consumer: {_SESSION_START_CONSUMER}"
        )
        assert not any("background_tasks" in p for p in read_saved_payloads(dump_dir)), (
            f"a {_SESSION_START_EVENT} payload now carries background_tasks {resume_drift}; consumer: "
            f"{_SESSION_START_CONSUMER}, which could write the records from that list instead of only removing them"
        )

        send_expecting_success(profile, ctx, agent_name, _AFTER_RESTART_PROMPT)
        wait_for_turn_to_end(profile, ctx, _AFTER_RESTART_PROMPT, turn_drift)
        assert _task_records(ctx) == (), (
            f"the first turn after the restart wrote the records {_task_records(ctx)}: claude resumed a task across "
            f"the restart {resume_drift}; consumer: {_SESSION_START_CONSUMER}, which removes the records on the "
            "premise that no task survives a restart"
        )
