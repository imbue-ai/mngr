"""Unit tests for the tk ticket parser."""

from __future__ import annotations

from imbue.minds_workspace_server.tickets_parser import parse_ticket_text


def test_parse_open_ticket_with_no_notes() -> None:
    """A freshly-created tk ticket has status open and no Notes section."""
    text = """---
id: tt-2efd
status: open
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
assignee: Test User
---
# Look through your recent changes to find the new theme
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.ticket_id == "tt-2efd"
    assert result.title == "Look through your recent changes to find the new theme"
    assert result.status == "open"
    assert result.created_at == "2026-04-28T01:17:08Z"
    assert result.summary is None
    assert result.summary_at is None


def test_parse_in_progress_ticket() -> None:
    text = """---
id: tt-2efd
status: in_progress
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
---
# Trace how the dark mode toggle picks a theme
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.status == "in_progress"
    assert result.summary is None


def test_parse_closed_ticket_with_summary_note() -> None:
    """The most recent note in the Notes section becomes the summary."""
    text = """---
id: tt-2efd
status: closed
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
---
# Look through your recent changes to find the new theme


## Notes

**2026-04-28T01:19:03Z**

Found a new "midnight" theme in your settings file. It defines colors for dark mode but isn't being registered with the theme switcher.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.status == "closed"
    assert result.summary is not None
    assert "midnight" in result.summary
    assert result.summary_at == "2026-04-28T01:19:03Z"


def test_parse_picks_latest_of_multiple_notes() -> None:
    """When multiple notes exist, the one with the latest ISO timestamp wins."""
    text = """---
id: tt-2efd
status: closed
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
---
# A multi-step task

## Notes

**2026-04-28T01:18:00Z**

First, some interim observation that should be ignored.

**2026-04-28T01:20:00Z**

Final summary that should be picked up.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.summary is not None
    assert result.summary.startswith("Final summary")
    assert result.summary_at == "2026-04-28T01:20:00Z"


def test_parse_returns_none_for_no_frontmatter() -> None:
    assert parse_ticket_text("# Just a title with no frontmatter\n") is None


def test_parse_returns_none_for_missing_id() -> None:
    text = """---
status: open
created: 2026-04-28T01:17:08Z
---
# Title
"""
    assert parse_ticket_text(text) is None


def test_parse_returns_none_for_invalid_status() -> None:
    text = """---
id: tt-abcd
status: bogus
created: 2026-04-28T01:17:08Z
---
# Title
"""
    assert parse_ticket_text(text) is None


def test_title_falls_back_to_id_when_no_h1() -> None:
    text = """---
id: tt-noheader
status: open
created: 2026-04-28T01:17:08Z
---
Some body text without an H1 heading.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.title == "tt-noheader"


def test_note_pattern_does_not_match_random_bold_text() -> None:
    """Bold text in the body that isn't a timestamped Notes header must not
    be misread as a summary."""
    text = """---
id: tt-prose
status: closed
created: 2026-04-28T01:17:08Z
---
# Title

Some **bold prose** here that is not a note.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.summary is None
