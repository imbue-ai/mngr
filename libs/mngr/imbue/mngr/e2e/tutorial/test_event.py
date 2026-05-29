"""Tests for the VIEWING EVENTS section of the tutorial.

Each test corresponds 1:1 to a tutorial script block. ``mngr event`` requires
a real agent to read events from; each test creates a sleep agent first.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# Every event is documented (in the tutorial) to be a JSON object with at least
# these four fields.
# Every event is guaranteed to be a JSON object carrying at least these fields
# (per the tutorial's documentation of the event stream).
_GUARANTEED_EVENT_FIELDS = ("event_id", "timestamp", "source", "type")


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
@pytest.mark.timeout(120)
def test_event_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # view all events for an agent
        mngr event my-task
        # all events are json objects that are guaranteed to have at least the following fields: "event_id", "timestamp", "source" and "type"
        # events are printed as JSONL (one JSON object per line), so you can easily pipe them to jq for filtering and formatting, or to other tools for monitoring and alerting
    """)
    _create_my_task(e2e, 100700)
    expect(e2e.run("mngr event my-task", comment="view all events for an agent")).to_succeed()


@pytest.mark.release
@pytest.mark.timeout(120)
def test_event_missing_agent_fails(e2e: E2eSession) -> None:
    """Unhappy path for the same block: events for a nonexistent agent.

    Without a matching agent, `mngr event` must fail with a clear error rather
    than silently printing an empty (success) event stream.
    """
    e2e.write_tutorial_block("""
        # view all events for an agent
        mngr event my-task
        # all events are json objects that are guaranteed to have at least the following fields: "event_id", "timestamp", "source" and "type"
        # events are printed as JSONL (one JSON object per line), so you can easily pipe them to jq for filtering and formatting, or to other tools for monitoring and alerting
    """)
    result = e2e.run("mngr event no-such-agent", comment="view events for a nonexistent agent")
    expect(result).to_fail()
    expect(result.stderr).to_contain("Could not find agent")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_event_follow(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # follow events in real time (like tail -f). Extremely useful for scripting.
        mngr event my-task --follow
    """)
    _create_my_task(e2e, 100701)
    # --follow streams indefinitely, so it never exits on its own. Wrap it with
    # `timeout 1`: when the stream is healthy it stays alive for the full second
    # and `timeout` kills it, exiting 124. Asserting on 124 (rather than masking
    # the exit code with `|| true`) proves the follow stream actually started and
    # kept running -- a startup crash would exit early with a different code.
    result = e2e.run("timeout 1 mngr event my-task --follow", comment="follow events in real time")
    expect(result).to_have_exit_code(124)
    # No error output should leak while streaming.
    expect(result.stderr).not_to_contain("Traceback")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
@pytest.mark.timeout(60)
def test_event_follow_filter_source(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # restrict the event stream to a specific type of event (source)
        # in this case we're looking at the "claude/common_transcript" events for a claude agent,
        # which shows the conversation messages in and out of the agent in a unified format
        mngr event my-task --follow claude/common_transcript
    """)
    _create_my_task(e2e, 100702)
    # --follow blocks indefinitely waiting for new events, so the harness kills
    # it after the timeout and reports exit code 124. Asserting 124 (rather than
    # swallowing the exit code) confirms the stream actually stayed open for the
    # full window instead of crashing or rejecting the source filter early.
    result = e2e.run(
        "mngr event my-task --follow claude/common_transcript",
        comment="restrict the event stream to a specific source",
        timeout=2.0,
    )
    expect(result).to_have_exit_code(124)


@pytest.mark.timeout(60)
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_event_tail(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only the last 20 events
        mngr event my-task --tail 20
    """)
    _create_my_task(e2e, 100703)
    result = e2e.run("mngr event my-task --tail 20", comment="show only the last 20 events")
    expect(result).to_succeed()
    # A freshly created `--no-connect` command agent has not produced any events
    # yet, so the stream is typically empty here. Regardless of how many events
    # are present, the output must honor the documented contract: each line is a
    # JSON object carrying the guaranteed fields, and --tail 20 caps the count.
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(events) <= 20, f"--tail 20 returned more than 20 events: {len(events)}"
    for event in events:
        missing = [field for field in _GUARANTEED_EVENT_FIELDS if field not in event]
        assert not missing, f"event is missing guaranteed field(s) {missing}: {event}"


@pytest.mark.timeout(60)
@pytest.mark.release
def test_event_tail_rejects_combination_with_head(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only the last 20 events
        mngr event my-task --tail 20
    """)
    # --tail and --head are mutually exclusive. Combining them is a usage error
    # that is rejected up front, before any agent is even resolved, so this needs
    # no agent and no remote resources.
    result = e2e.run(
        "mngr event my-task --tail 20 --head 10",
        comment="--tail cannot be combined with --head",
    )
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_contain("Cannot specify both --head and --tail")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_event_head(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only the first 10 events
        mngr event my-task --head 10
    """)
    _create_my_task(e2e, 100704)
    result = e2e.run("mngr event my-task --head 10", comment="show only the first 10 events")
    expect(result).to_succeed()
    # Verify the --head contract: events are emitted as JSONL, capped at the
    # requested count, and each one is a JSON object carrying the documented
    # guaranteed fields. (A freshly-created sleep agent may legitimately have
    # fewer than 10 events, so we assert "at most 10", not "exactly 10".)
    event_lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(event_lines) <= 10, f"--head 10 returned {len(event_lines)} events"
    for line in event_lines:
        event = json.loads(line)
        for field in _GUARANTEED_EVENT_FIELDS:
            assert field in event, f"event missing guaranteed field {field!r}: {line}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_event_include_filter_invalid_cel(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: a malformed CEL expression must
    # be rejected with a clear error rather than silently producing no output.
    # The happy-path test above passes vacuously (a command agent has no events,
    # so the filter matches nothing), so this is the test that actually exercises
    # the --include CEL compilation path.
    e2e.write_tutorial_block("""
        # include only events matching a CEL expression
        mngr event my-task --include 'type == "user_message"'
    """)
    _create_my_task(e2e, 100706)
    result = e2e.run(
        "mngr event my-task --include 'type ==== broken(((' ",
        comment="reject a malformed CEL include expression",
    )
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_contain("Invalid include filter expression")
