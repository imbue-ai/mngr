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


def _ticket_text(
    ticket_id: str,
    status: str,
    *,
    title: str = "Sample task",
    created: str = "2026-04-28T01:00:00Z",
    notes: str | None = None,
) -> str:
    """Build a tk-shaped ticket body. Centralizes the boilerplate frontmatter
    so individual tests only describe the parts that actually vary
    (id / status / notes / title)."""
    body = f"""---
id: {ticket_id}
status: {status}
deps: []
links: []
created: {created}
type: task
priority: 2
---
# {title}
"""
    if notes is not None:
        body += f"\n## Notes\n\n{notes}\n"
    return body


def _write_ticket_with_status(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    *,
    title: str = "Sample task",
    notes: str | None = None,
) -> Path:
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(_ticket_text(ticket_id, status, title=title, notes=notes))
    return path


def test_silent_when_tickets_dir_missing(tmp_path: Path) -> None:
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tmp_path / ".tickets", cb)
    assert watcher.get_all_events() == []


def test_scan_skips_files_with_invalid_utf8(tmp_path: Path) -> None:
    """A *.md file containing non-UTF-8 bytes must not crash the watcher;
    it should be skipped silently like any other unreadable file. Without
    this, a single malformed file would propagate UnicodeDecodeError up
    through _scan() and kill the watcher's background thread."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tt-good", "open", title="Valid ticket")
    bad_file = tickets_dir / "tt-bad.md"
    bad_file.write_bytes(b"---\nid: tt-bad\nstatus: open\n---\n# \xff\xfe\xfd not utf-8\n")

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    events = watcher.get_all_events()
    # The valid ticket comes through; the malformed one is silently skipped.
    assert [e["ticket_id"] for e in events] == ["tt-good"]


def test_open_ticket_emits_one_event_with_created_at_timestamp(tmp_path: Path) -> None:
    """A freshly-discovered open ticket emits a single open event whose
    timestamp comes from the frontmatter `created` field (truthful)."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tt-aaaa", "open", title="Hello world")
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
    _write_ticket_with_status(tickets_dir, "tt-bbbb", "in_progress", title="In progress task")
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
    _write_ticket_with_status(
        tickets_dir,
        "tt-cccc",
        "closed",
        title="Done task",
        notes="**2026-04-28T01:05:00Z**\n\nFinal summary text for this task.",
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
    _write_ticket_with_status(
        tickets_dir,
        "tt-dddd",
        "in_progress",
        title="Still working",
        notes="**2026-04-28T01:02:00Z**\n\nInterim note that should not appear as a summary yet.",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    events = watcher.get_all_events()
    assert len(events) == 1
    assert events[0]["summary"] is None
    assert events[0]["summary_at"] is None


def test_repeated_get_all_events_is_idempotent(tmp_path: Path) -> None:
    """Re-calling get_all_events() against an unchanged directory yields
    the same cumulative history. This is the contract _get_combined_events
    in server.py relies on: every page reload re-issues GET /events and
    expects the full event list back, not just deltas since the last poll."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tt-eeee", "open", title="Stable")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)
    first = watcher.get_all_events()
    assert [e["event_id"] for e in first] == ["tt-eeee-open"]
    second = watcher.get_all_events()
    assert second == first


def test_lifecycle_accumulates_one_event_per_observed_transition(tmp_path: Path) -> None:
    """A ticket the watcher observes through its full lifecycle (open
    -> in_progress -> closed) accumulates exactly three events in the
    cumulative history, one per observed transition. get_all_events()
    returns the full accumulated list each call."""
    tickets_dir = tmp_path / ".tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / "tt-ffff.md"

    path.write_text(_ticket_text("tt-ffff", "open", title="Lifecycle test"))

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", tickets_dir, cb)

    events1 = watcher.get_all_events()
    assert [e["event_id"] for e in events1] == ["tt-ffff-open"]

    path.write_text(_ticket_text("tt-ffff", "in_progress", title="Lifecycle test"))
    events2 = watcher.get_all_events()
    assert [e["event_id"] for e in events2] == ["tt-ffff-open", "tt-ffff-in_progress"]

    path.write_text(
        _ticket_text(
            "tt-ffff",
            "closed",
            title="Lifecycle test",
            notes="**2026-04-28T01:10:00Z**\n\nAll done.",
        )
    )
    events3 = watcher.get_all_events()
    assert [e["event_id"] for e in events3] == ["tt-ffff-open", "tt-ffff-in_progress", "tt-ffff-closed"]
    assert events3[-1]["summary"] == "All done."
