from collections.abc import Sequence
from typing import Any

from imbue.imbue_common.pure import pure
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import section_label


@pure
def _group_entries_by_section(
    snapshot: BoardSnapshot,
    section_order: Sequence[BoardSection],
) -> list[tuple[BoardSection, list[AgentBoardEntry]]]:
    """Group entries by section in display order, mirroring the TUI.

    Sections are returned in ``section_order``; empty sections are omitted, and
    entries within a section keep their snapshot order. Entries whose section is
    not in ``section_order`` are dropped -- the same omission the TUI applies to
    a customized ``section_order`` -- so the JSON represents what the board shows.
    """
    by_section: dict[BoardSection, list[AgentBoardEntry]] = {}
    for entry in snapshot.entries:
        by_section.setdefault(entry.section, []).append(entry)
    return [(section, by_section[section]) for section in section_order if by_section.get(section)]


@pure
def board_snapshot_to_json(
    snapshot: BoardSnapshot,
    columns: Sequence[tuple[str, str]],
    section_order: Sequence[BoardSection],
) -> dict[str, Any]:
    """Build the structured JSON representation of a board snapshot.

    ``columns`` is the ordered ``(field_key, header)`` layout and ``section_order``
    the section display order (both from ``tui.resolve_board_layout``). Each entry
    is dumped with ``model_dump(mode="json")``; its ``fields`` are ``SerializeAsAny``
    so the full typed payload (PR number, CI status, etc.) is preserved alongside
    the pre-rendered ``cells``.
    """
    sections = [
        {
            "section": section.value,
            "label": section_label(section),
            "entries": [entry.model_dump(mode="json") for entry in entries],
        }
        for section, entries in _group_entries_by_section(snapshot, section_order)
    ]
    return {
        "columns": [{"key": key, "header": header} for key, header in columns],
        "sections": sections,
        "errors": list(snapshot.errors),
        "fetch_time_seconds": snapshot.fetch_time_seconds,
    }


@pure
def board_snapshot_to_jsonl_entries(
    snapshot: BoardSnapshot,
    section_order: Sequence[BoardSection],
) -> list[dict[str, Any]]:
    """Flatten a board snapshot into one JSON object per agent entry.

    Entries are ordered by section (as the board shows them) then by snapshot
    order within a section. Used for ``--format jsonl``, where each line is a
    self-contained agent record (it carries its own ``section``); the column and
    section-order metadata that JSON carries has no place in a flat stream.
    """
    return [
        entry.model_dump(mode="json")
        for _section, entries in _group_entries_by_section(snapshot, section_order)
        for entry in entries
    ]
