"""Parse tk ticket markdown files into task state.

Used by the AgentTicketsWatcher to convert `.tickets/<id>.md` files into
common task events that flow through the same pipeline as session events
(see session_parser.py for the analogous session-side flow).

A tk ticket file looks like:

    ---
    id: tt-2efd
    status: in_progress
    deps: []
    links: []
    created: 2026-04-28T01:17:08Z
    type: task
    priority: 2
    assignee: ...
    ---
    # Look through your recent changes to find the new theme

    Optional description body...

    ## Notes

    **2026-04-28T01:19:03Z**

    Found a new "midnight" theme in your settings file...

The most recent note in the `## Notes` section is treated as the ticket's
current "summary" (rendered under the task in the chat progress view when
the ticket is closed). Earlier notes are ignored.
"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

_VALID_STATUSES = frozenset({"open", "in_progress", "closed"})

# Matches a single timestamped note inside the `## Notes` section:
#   **2026-04-28T01:19:03Z**
#   <blank line>
#   <text body until next note marker or EOF>
_NOTE_PATTERN = re.compile(
    r"\*\*(?P<ts>[0-9TZ:\-]+)\*\*\s*\n\n(?P<body>.+?)(?=\n\s*\*\*[0-9TZ:\-]+\*\*|\Z)",
    re.DOTALL,
)


class TicketState(FrozenModel):
    """Parsed snapshot of a tk ticket file at a single point in time."""

    ticket_id: str = Field(description="Ticket id from frontmatter")
    title: str = Field(description="H1 title from the body")
    status: str = Field(description="open | in_progress | closed")
    created_at: str = Field(description="frontmatter `created` field, ISO-8601")
    summary: str | None = Field(description="Most recent note text, or None")
    summary_at: str | None = Field(description="Timestamp of the most recent note, or None")


def parse_ticket_text(text: str) -> TicketState | None:
    """Parse a tk ticket markdown body. Returns None if the file isn't a
    valid tk ticket (no frontmatter, missing required fields, etc)."""
    if not text.startswith("---\n"):
        return None
    front_end = text.find("\n---\n", 4)
    if front_end < 0:
        return None
    frontmatter = text[4:front_end]
    body = text[front_end + len("\n---\n") :]

    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip()

    ticket_id = fields.get("id", "")
    status = fields.get("status", "")
    created_at = fields.get("created", "")

    if not ticket_id or status not in _VALID_STATUSES:
        return None

    # Title is the first H1 in the body. Falls back to ticket_id if absent.
    title = ticket_id
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip() or ticket_id
            break

    summary, summary_at = _extract_latest_note(body)

    return TicketState(
        ticket_id=ticket_id,
        title=title,
        status=status,
        created_at=created_at,
        summary=summary,
        summary_at=summary_at,
    )


def parse_ticket_file(path: Path) -> TicketState | None:
    """Read a ticket file from disk and parse it. Returns None on read
    failure or invalid content."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Skipping unreadable ticket file {}: {}", path, e)
        return None
    return parse_ticket_text(text)


def _extract_latest_note(body: str) -> tuple[str | None, str | None]:
    """Find the most-recent timestamped note in the body's `## Notes` section.

    Returns (note_text, note_timestamp), or (None, None) if there are no
    notes. "Most recent" is determined by lexicographic timestamp order,
    which matches ISO-8601 chronological order for the format tk emits.
    """
    notes_marker = "## Notes"
    notes_idx = body.find(notes_marker)
    if notes_idx < 0:
        return (None, None)
    notes_section = body[notes_idx + len(notes_marker) :]

    latest_text: str | None = None
    latest_ts: str | None = None
    for match in _NOTE_PATTERN.finditer(notes_section):
        ts = match.group("ts")
        text = match.group("body").strip()
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_text = text
    return (latest_text, latest_ts)
