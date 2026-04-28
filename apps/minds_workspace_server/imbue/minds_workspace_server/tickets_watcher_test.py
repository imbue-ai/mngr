"""Unit tests for AgentTicketsWatcher.

These tests exercise the get_all_events() path (the synchronous side of
the watcher used to seed initial state); the live polling thread is
covered indirectly because get_all_events delegates to the same _scan().
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from imbue.minds_workspace_server.tickets_watcher import AgentTicketsWatcher


def _capture() -> tuple[list[tuple[str, list[dict[str, Any]]]], Any]:
    """Returns (calls, callback) for use as the watcher's on_events arg."""
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    def cb(agent_id: str, events: list[dict[str, Any]]) -> None:
        calls.append((agent_id, events))

    return calls, cb


def _write_ticket(tickets_dir: Path, content: str) -> Path:
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / f"{content.split('id: ')[1].split(chr(10))[0].strip()}.md"
    path.write_text(content)
    return path


def test_silent_when_tickets_dir_missing(tmp_path: Path) -> None:
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tmp_path / ".tickets", cb)
    assert watcher.get_all_events() == []


def test_open_ticket_emits_one_event(tmp_path: Path) -> None:
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        """---
id: tt-aaaa
status: open
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# Hello world
""",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    events = watcher.get_all_events()
    assert len(events) == 1
    assert events[0]["type"] == "task_event"
    assert events[0]["event_id"] == "tt-aaaa-open"
    assert events[0]["status"] == "open"
    assert events[0]["timestamp"] == "2026-04-28T01:00:00Z"
    assert events[0]["title"] == "Hello world"


def test_in_progress_ticket_emits_open_and_in_progress(tmp_path: Path) -> None:
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        """---
id: tt-bbbb
status: in_progress
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# In progress task
""",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    events = watcher.get_all_events()
    statuses = [(e["status"], e["event_id"]) for e in events]
    assert statuses == [
        ("open", "tt-bbbb-open"),
        ("in_progress", "tt-bbbb-in_progress"),
    ]
    assert events[0]["timestamp"] == "2026-04-28T01:00:00Z"
    # in_progress timestamp falls back to mtime, which is "now"-ish from
    # the file write -- we don't assert the exact value, just that it's
    # >= created_at and parseable.
    assert events[1]["timestamp"] >= events[0]["timestamp"]


def test_closed_ticket_emits_three_events_with_summary(tmp_path: Path) -> None:
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        """---
id: tt-cccc
status: closed
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# Done task

## Notes

**2026-04-28T01:05:00Z**

Final summary text for this task.
""",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    events = watcher.get_all_events()
    assert [e["status"] for e in events] == ["open", "in_progress", "closed"]
    closed = events[-1]
    assert closed["event_id"] == "tt-cccc-closed"
    assert closed["summary"] == "Final summary text for this task."
    assert closed["summary_at"] == "2026-04-28T01:05:00Z"


def test_summary_only_present_on_closed_event(tmp_path: Path) -> None:
    """A ticket with notes that's still in_progress must not leak the
    note text into the in_progress event (the frontend treats summary
    as the closing-out report)."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        """---
id: tt-dddd
status: in_progress
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# Still working

## Notes

**2026-04-28T01:02:00Z**

Interim note that should not appear as a summary yet.
""",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    events = watcher.get_all_events()
    for e in events:
        assert e["summary"] is None
        assert e["summary_at"] is None


def test_get_all_events_returns_cumulative_history(tmp_path: Path) -> None:
    """Repeated get_all_events() returns the full ordered history every
    time -- it is the route-handler path used to seed initial state, not
    a live-changes-only feed."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        """---
id: tt-eeee
status: open
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# Stable
""",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    first = watcher.get_all_events()
    assert [e["event_id"] for e in first] == ["tt-eeee-open"]
    second = watcher.get_all_events()
    assert [e["event_id"] for e in second] == ["tt-eeee-open"]


def test_status_progression_through_initial_load_path(tmp_path: Path) -> None:
    """Each get_all_events() call returns the full current event list;
    when the ticket transitions, new events appear in the cumulative
    history. This is the chat-load-time view."""
    tickets_dir = tmp_path / ".tickets"
    path = tickets_dir / "tt-ffff.md"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    path.write_text(
        """---
id: tt-ffff
status: open
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# Lifecycle test
"""
    )

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)

    events1 = watcher.get_all_events()
    assert [e["event_id"] for e in events1] == ["tt-ffff-open"]

    path.write_text(
        """---
id: tt-ffff
status: in_progress
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# Lifecycle test
"""
    )
    events2 = watcher.get_all_events()
    assert [e["event_id"] for e in events2] == ["tt-ffff-open", "tt-ffff-in_progress"]

    path.write_text(
        """---
id: tt-ffff
status: closed
deps: []
links: []
created: 2026-04-28T01:00:00Z
type: task
priority: 2
---
# Lifecycle test

## Notes

**2026-04-28T01:10:00Z**

All done.
"""
    )
    events3 = watcher.get_all_events()
    assert [e["event_id"] for e in events3] == ["tt-ffff-open", "tt-ffff-in_progress", "tt-ffff-closed"]
    assert events3[-1]["summary"] == "All done."
