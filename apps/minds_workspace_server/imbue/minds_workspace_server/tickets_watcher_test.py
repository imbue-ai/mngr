"""Unit tests for AgentTicketsWatcher.

The watcher emits one event per OBSERVED state transition. On replay
(the watcher is started against a directory whose tickets are already
past-`open`), only the current status emits an event -- earlier
transitions weren't observed and are not synthesized.
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


def test_open_ticket_emits_one_event_with_created_at_timestamp(tmp_path: Path) -> None:
    """A freshly-discovered open ticket emits a single open event whose
    timestamp comes from the frontmatter `created` field (truthful)."""
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
    assert events[0]["event_id"] == "tt-aaaa-open"
    assert events[0]["status"] == "open"
    assert events[0]["timestamp"] == "2026-04-28T01:00:00Z"
    assert events[0]["title"] == "Hello world"


def test_replayed_in_progress_ticket_emits_only_current_status(tmp_path: Path) -> None:
    """A ticket discovered already at in_progress was not observed
    transitioning from open -- so we emit a single in_progress event,
    NOT a synthetic open event."""
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
    assert len(events) == 1
    assert events[0]["event_id"] == "tt-bbbb-in_progress"
    # created_at field still carries the frontmatter value -- the
    # frontend uses that for turn attribution and the "ticket existed
    # since" lower bound.
    assert events[0]["created_at"] == "2026-04-28T01:00:00Z"


def test_replayed_closed_ticket_emits_only_closed_event_with_summary(tmp_path: Path) -> None:
    """A ticket discovered already at closed emits one closed event;
    no synthetic in_progress is generated. Summary still rides on the
    closed event."""
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
    assert len(events) == 1
    assert events[0]["event_id"] == "tt-cccc-closed"
    assert events[0]["status"] == "closed"
    assert events[0]["summary"] == "Final summary text for this task."
    assert events[0]["summary_at"] == "2026-04-28T01:05:00Z"


def test_summary_only_on_closed_event(tmp_path: Path) -> None:
    """A ticket with notes still in_progress: no summary leaks; the
    in_progress event's summary field is None."""
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
    assert len(events) == 1
    assert events[0]["summary"] is None
    assert events[0]["summary_at"] is None


def test_repeated_scan_is_idempotent(tmp_path: Path) -> None:
    """Re-scanning an unchanged directory emits no new events."""
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
    assert len(first) == 1
    second = watcher.get_all_events()
    assert second == []


def test_lifecycle_emits_one_event_per_observed_transition(tmp_path: Path) -> None:
    """A ticket the watcher observes through its full lifecycle (open
    -> in_progress -> closed) emits exactly three events, one per
    observed transition."""
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
    assert [e["event_id"] for e in events2] == ["tt-ffff-in_progress"]

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
    assert [e["event_id"] for e in events3] == ["tt-ffff-closed"]
    assert events3[0]["summary"] == "All done."
