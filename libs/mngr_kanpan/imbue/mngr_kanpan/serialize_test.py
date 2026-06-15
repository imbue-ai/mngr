from datetime import datetime
from datetime import timezone

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.serialize import board_snapshot_to_json
from imbue.mngr_kanpan.serialize import board_snapshot_to_jsonl_entries
from imbue.mngr_kanpan.testing import make_board_snapshot

_NOW = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# Column layout in display order, as tui.resolve_board_layout would produce it.
_COLUMNS: tuple[tuple[str, str], ...] = (("name", "NAME"), ("state", "STATE"), ("pr", "PR"))

_SECTION_ORDER: tuple[BoardSection, ...] = (
    BoardSection.PR_MERGED,
    BoardSection.STILL_COOKING,
    BoardSection.MUTED,
)


def _make_entry(
    name: str,
    section: BoardSection,
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
) -> AgentBoardEntry:
    return AgentBoardEntry(
        name=AgentName(name),
        state=state,
        provider_name=ProviderInstanceName("local"),
        section=section,
    )


def test_board_snapshot_to_json_top_level_shape() -> None:
    snapshot = make_board_snapshot(
        entries=(_make_entry("a", BoardSection.STILL_COOKING),),
        errors=("boom",),
        fetch_time_seconds=2.5,
    )
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    assert result["columns"] == [
        {"key": "name", "header": "NAME"},
        {"key": "state", "header": "STATE"},
        {"key": "pr", "header": "PR"},
    ]
    assert result["errors"] == ["boom"]
    assert result["fetch_time_seconds"] == 2.5


def test_board_snapshot_to_json_groups_by_section_in_order() -> None:
    # Entries given out of section_order; output must follow section_order.
    snapshot = make_board_snapshot(
        entries=(
            _make_entry("cooking", BoardSection.STILL_COOKING),
            _make_entry("merged", BoardSection.PR_MERGED),
        )
    )
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    section_keys = [s["section"] for s in result["sections"]]
    assert section_keys == ["PR_MERGED", "STILL_COOKING"]


def test_board_snapshot_to_json_omits_empty_sections() -> None:
    snapshot = make_board_snapshot(entries=(_make_entry("only", BoardSection.PR_MERGED),))
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    assert [s["section"] for s in result["sections"]] == ["PR_MERGED"]


def test_board_snapshot_to_json_drops_sections_not_in_order() -> None:
    # PR_CLOSED is absent from _SECTION_ORDER, so its entry is dropped (mirrors
    # the TUI omitting sections a user left out of section_order).
    snapshot = make_board_snapshot(
        entries=(
            _make_entry("kept", BoardSection.MUTED),
            _make_entry("dropped", BoardSection.PR_CLOSED),
        )
    )
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    names = [e["name"] for s in result["sections"] for e in s["entries"]]
    assert names == ["kept"]


def test_board_snapshot_to_json_section_labels() -> None:
    snapshot = make_board_snapshot(
        entries=(
            _make_entry("m", BoardSection.PR_MERGED),
            _make_entry("mute", BoardSection.MUTED),
        )
    )
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    labels = {s["section"]: s["label"] for s in result["sections"]}
    assert labels["PR_MERGED"] == "Done - PR merged"
    assert labels["MUTED"] == "Muted"


def test_board_snapshot_to_json_preserves_order_within_section() -> None:
    snapshot = make_board_snapshot(
        entries=(
            _make_entry("first", BoardSection.STILL_COOKING),
            _make_entry("second", BoardSection.STILL_COOKING),
        )
    )
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    entries = result["sections"][0]["entries"]
    assert [e["name"] for e in entries] == ["first", "second"]


def test_board_snapshot_to_json_entry_includes_cells_and_metadata() -> None:
    entry = AgentBoardEntry(
        name=AgentName("agent-x"),
        state=AgentLifecycleState.WAITING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/agent-x",
        section=BoardSection.PR_MERGED,
        cells={"pr": CellDisplay(text="#7", url="https://example/pr/7", color="light green")},
    )
    snapshot = make_board_snapshot(entries=(entry,))
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    dumped = result["sections"][0]["entries"][0]
    assert dumped["name"] == "agent-x"
    assert dumped["state"] == "WAITING"
    assert dumped["branch"] == "mngr/agent-x"
    assert dumped["section"] == "PR_MERGED"
    assert dumped["cells"]["pr"] == {"text": "#7", "url": "https://example/pr/7", "color": "light green"}


def test_board_snapshot_to_json_serializes_full_field_payload() -> None:
    # SerializeAsAny: the concrete PrField subclass fields must survive the dump,
    # not just the FieldValue base `created`.
    pr = PrField(
        number=99,
        title="My PR",
        state=PrState.MERGED,
        url="https://github.com/org/repo/pull/99",
        head_branch="mngr/agent-x",
        is_draft=False,
        created=_NOW,
    )
    entry = AgentBoardEntry(
        name=AgentName("agent-x"),
        state=AgentLifecycleState.DONE,
        provider_name=ProviderInstanceName("local"),
        section=BoardSection.PR_MERGED,
        fields={"pr": pr},
    )
    snapshot = make_board_snapshot(entries=(entry,))
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    pr_field = result["sections"][0]["entries"][0]["fields"]["pr"]
    assert pr_field["kind"] == "pr"
    assert pr_field["number"] == 99
    assert pr_field["state"] == "MERGED"
    assert pr_field["created"] == "2025-01-01T00:00:00Z"


def test_jsonl_entries_flat_and_ordered() -> None:
    snapshot = make_board_snapshot(
        entries=(
            _make_entry("cooking", BoardSection.STILL_COOKING),
            _make_entry("merged", BoardSection.PR_MERGED),
            _make_entry("muted", BoardSection.MUTED),
        )
    )
    entries = board_snapshot_to_jsonl_entries(snapshot, _SECTION_ORDER)
    # Flattened in section_order, not snapshot order.
    assert [e["name"] for e in entries] == ["merged", "cooking", "muted"]


def test_jsonl_entries_drops_sections_not_in_order() -> None:
    snapshot = make_board_snapshot(
        entries=(
            _make_entry("kept", BoardSection.PR_MERGED),
            _make_entry("dropped", BoardSection.PR_CLOSED),
        )
    )
    entries = board_snapshot_to_jsonl_entries(snapshot, _SECTION_ORDER)
    assert [e["name"] for e in entries] == ["kept"]


def test_empty_snapshot_yields_no_sections() -> None:
    snapshot = make_board_snapshot()
    result = board_snapshot_to_json(snapshot, _COLUMNS, _SECTION_ORDER)
    assert result["sections"] == []
    assert board_snapshot_to_jsonl_entries(snapshot, _SECTION_ORDER) == []
