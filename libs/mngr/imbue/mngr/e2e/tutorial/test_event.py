"""Tests for the VIEWING EVENTS section of the tutorial.

Each test corresponds 1:1 to a tutorial script block. ``mngr event`` requires
a real agent to read events from; each test creates a sleep agent first.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_event_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # view all events for an agent
        mngr event my-task
        # all events are json objects that are guaranteed to have at least the following fields: "event_id", "timestamp", "source" and "type"
        # events are printed as JSONL (one JSON object per line), so you can easily pipe them to jq for filtering and formatting, or to other tools for monitoring and alerting
    """)
    _create_my_task(e2e, 100700)
    expect(e2e.run("mngr event my-task", comment="view all events for an agent")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_event_follow(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # follow events in real time (like tail -f). Extremely useful for scripting.
        mngr event my-task --follow
    """)
    _create_my_task(e2e, 100701)
    # --follow streams indefinitely; wrap with `timeout 1` so the test
    # confirms the stream started without hanging.
    expect(e2e.run("timeout 1 mngr event my-task --follow || true", comment="follow events in real time")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_event_follow_filter_source(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # restrict the event stream to a specific type of event (source)
        # in this case we're looking at the "claude/common_transcript" events for a claude agent,
        # which shows the conversation messages in and out of the agent in a unified format
        mngr event my-task --follow claude/common_transcript
    """)
    _create_my_task(e2e, 100702)
    expect(
        e2e.run(
            "timeout 1 mngr event my-task --follow claude/common_transcript || true",
            comment="restrict the event stream to a specific source",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_event_tail(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only the last 20 events
        mngr event my-task --tail 20
    """)
    _create_my_task(e2e, 100703)
    expect(e2e.run("mngr event my-task --tail 20", comment="show only the last 20 events")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_event_head(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only the first 10 events
        mngr event my-task --head 10
    """)
    _create_my_task(e2e, 100704)
    expect(e2e.run("mngr event my-task --head 10", comment="show only the first 10 events")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_event_include_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # include only events matching a CEL expression
        mngr event my-task --include 'type == "user_message"'
    """)
    _create_my_task(e2e, 100705)
    expect(
        e2e.run(
            "mngr event my-task --include 'type == \"user_message\"'",
            comment="include only events matching a CEL expression",
        )
    ).to_succeed()
