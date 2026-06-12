"""Tests for the VIEWING EVENTS section of the tutorial.

Each test corresponds 1:1 to a tutorial script block. ``mngr event`` requires
a real agent to read events from; each test creates a sleep agent first.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# The tutorial documents that every event is a JSON object guaranteed to carry
# at least these four fields.
_GUARANTEED_EVENT_FIELDS = ("event_id", "timestamp", "source", "type")


def _parse_jsonl_events(stdout: str) -> list[dict[str, object]]:
    """Parse `mngr event` stdout as JSONL, asserting each line is a JSON object."""
    events: list[dict[str, object]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert isinstance(parsed, dict), f"Expected each event line to be a JSON object, got: {line!r}"
        events.append(parsed)
    return events


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    # Use the default (local) provider, matching the provider-agnostic tutorial.
    # A local agent runs inside a local tmux session (`tmux` mark) and rsyncs its
    # work dir into place (`rsync` mark). The default provider never touches
    # Modal, so these tests deliberately do NOT carry the `modal` mark.
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
            timeout=120.0,
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
    result = e2e.run("mngr event my-task", comment="view all events for an agent", timeout=60.0)
    expect(result).to_succeed()
    # The tutorial promises the output is clean JSONL (one JSON object per line)
    # so it can be piped to jq. Verify that contract: every line on stdout must
    # parse as a JSON object, with no warnings or log lines leaking in. A bare
    # `sleep` command agent legitimately has no events yet, so the stream may be
    # empty; whenever events ARE present they must carry the four documented
    # guaranteed fields.
    events = _parse_jsonl_events(result.stdout)
    for event in events:
        for field in _GUARANTEED_EVENT_FIELDS:
            assert field in event, f"Event missing guaranteed field {field!r}: {event!r}"


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
    # --follow polls for new events forever (it blocks after emitting any
    # backlog), so wrap it in `timeout` to stop it. A clean timeout kill exits
    # 124; observing that exit code is the meaningful assertion here: it proves
    # --follow kept the stream open instead of emitting events and exiting on its
    # own the way the non-follow `mngr event` command does.
    result = e2e.run("timeout 3 mngr event my-task --follow", comment="follow events in real time")
    expect(result).to_have_exit_code(124)


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_event_follow_filter_source(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # restrict the event stream to a specific type of event (source)
        # in this case we're looking at the "claude/common_transcript" events for a claude agent,
        # which shows the conversation messages in and out of the agent in a unified format
        mngr event my-task --follow claude/common_transcript
    """)
    _create_my_task(e2e, 100702)
    # --follow streams indefinitely, so `timeout 1` kills it after 1s and the
    # shell reports exit code 124. Asserting on 124 (rather than swallowing the
    # exit code with `|| true`) confirms the stream actually started and kept
    # running instead of crashing early -- any other exit code is a failure.
    result = e2e.run(
        "timeout 1 mngr event my-task --follow claude/common_transcript",
        comment="restrict the event stream to a specific source",
    )
    expect(result).to_have_exit_code(124)
    # The sleep command agent emits no "claude/common_transcript" events, so the
    # source filter must exclude everything and produce no output.
    expect(result.stdout).to_be_empty()


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
    # A freshly created `sleep` command agent may not have produced any events
    # yet, so we don't require output. But whatever --tail emits must respect its
    # contract: at most 20 events, each a JSONL object carrying the four
    # guaranteed fields (event_id, timestamp, source, type).
    events = _parse_jsonl_events(result.stdout)
    assert len(events) <= 20, f"--tail 20 returned {len(events)} events:\n{result.stdout}"
    for event in events:
        for field in _GUARANTEED_EVENT_FIELDS:
            assert field in event, f"Event missing guaranteed field {field!r}: {event!r}"


# Creating a local command agent (rsync + tmux) plus reading its events takes
# well over the default 10s pytest-timeout; give it ample headroom. The agent is
# created locally, so this test never invokes modal (no @pytest.mark.modal).
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
    # A freshly created `sleep` command agent may not have produced any events
    # yet, so we don't require output. But whatever --head emits must respect
    # its contract: at most 10 events, each a JSONL object carrying the four
    # guaranteed fields (event_id, timestamp, source, type).
    events = _parse_jsonl_events(result.stdout)
    assert len(events) <= 10, f"--head 10 returned {len(events)} events:\n{result.stdout}"
    for event in events:
        for field in _GUARANTEED_EVENT_FIELDS:
            assert field in event, f"Event missing guaranteed field {field!r}: {event!r}"

    # --head selects the FIRST events; the full stream is a superset whose
    # leading prefix matches --head exactly. Verify --head returns the earliest
    # events (not the tail) by comparing against the unfiltered stream.
    unfiltered = e2e.run("mngr event my-task", comment="read the full unfiltered event stream")
    expect(unfiltered).to_succeed()
    all_events = _parse_jsonl_events(unfiltered.stdout)
    head_ids = [event["event_id"] for event in events]
    all_ids = [event["event_id"] for event in all_events]
    assert head_ids == all_ids[: len(head_ids)], (
        f"--head 10 must return the leading prefix of the full stream; got {head_ids!r} vs {all_ids!r}"
    )


# Unhappy path for the --head block: --head and --tail select opposite ends of
# the stream, so combining them is rejected before any events are read.
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_event_head_conflicts_with_tail(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only the first 10 events
        mngr event my-task --head 10
    """)
    _create_my_task(e2e, 100706)
    result = e2e.run(
        "mngr event my-task --head 10 --tail 20",
        comment="--head and --tail cannot be combined",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("Cannot specify both --head and --tail")
    # The conflict is rejected during argument validation, before any events are
    # read, so nothing should be emitted to stdout.
    expect(result.stdout).to_be_empty()


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

    # The exact tutorial command. Whatever the filter returns, every event must
    # satisfy the CEL predicate -- the filter must never let a non-matching event
    # through. (A command/sleep agent emits no "user_message" events, so in
    # practice the result is empty.)
    filtered = e2e.run(
        "mngr event my-task --include 'type == \"user_message\"'",
        comment="include only events matching a CEL expression",
    )
    expect(filtered).to_succeed()
    filtered_events = _parse_jsonl_events(filtered.stdout)
    assert all(event["type"] == "user_message" for event in filtered_events)

    # The filtered result must be a subset of the full, unfiltered stream: the
    # filter is only allowed to drop events, never invent or alter them.
    unfiltered = e2e.run("mngr event my-task", comment="read the full unfiltered event stream")
    expect(unfiltered).to_succeed()
    unfiltered_ids = {event["event_id"] for event in _parse_jsonl_events(unfiltered.stdout)}
    assert {event["event_id"] for event in filtered_events} <= unfiltered_ids


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_event_include_filter_rejects_invalid_cel(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: a syntactically invalid CEL
    # expression must fail loudly rather than silently returning all (or no)
    # events.
    e2e.write_tutorial_block("""
        # include only events matching a CEL expression
        mngr event my-task --include 'type == "user_message"'
    """)
    _create_my_task(e2e, 100706)
    result = e2e.run(
        "mngr event my-task --include 'type ==='",
        comment="an invalid CEL expression is rejected",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("Invalid include filter expression")
    # "Fail loudly" also means it must not silently leak events: a rejected
    # filter produces no event output on stdout at all.
    expect(result.stdout).to_be_empty()
