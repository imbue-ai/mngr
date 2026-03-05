from pathlib import Path
from typing import Any

from urwid.widget.attr_map import AttrMap
from urwid.widget.columns import Columns
from urwid.widget.text import Text

from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSection
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import PendingMark
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.testing import make_pr_info
from imbue.mng_kanpan.tui import _KanpanState
from imbue.mng_kanpan.tui import _advance_focus
from imbue.mng_kanpan.tui import _build_board_widgets
from imbue.mng_kanpan.tui import _carry_forward_pr_data
from imbue.mng_kanpan.tui import _classify_entry
from imbue.mng_kanpan.tui import _get_name_cell_markup
from imbue.mng_kanpan.tui import _is_safe_to_delete
from imbue.mng_kanpan.tui import _toggle_mark
from imbue.mng_kanpan.tui import _unmark_all
from imbue.mng_kanpan.tui import _unmark_focused


class _Stub:
    """A permissive stub that accepts any attribute access or method call."""

    def __getattr__(self, name: str) -> "_Stub":
        return _Stub()

    def __call__(self, *args: Any, **kwargs: Any) -> "_Stub":
        return _Stub()

    def __setattr__(self, name: str, value: Any) -> None:
        pass


def _stub() -> Any:
    """Return an opaque stub for fields that aren't exercised by tests."""
    return _Stub()


# --- Helpers ---


def _make_entry(
    name: str = "agent-1",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    pr_state: PrState | None = None,
    work_dir: Path | None = None,
    is_muted: bool = False,
) -> AgentBoardEntry:
    """Create an AgentBoardEntry for testing."""
    pr = None
    if pr_state is not None:
        pr = PrInfo(
            number=1,
            title="test",
            state=pr_state,
            url="https://github.com/org/repo/pull/1",
            head_branch="mng/test",
            check_status=CheckStatus.PASSING,
            is_draft=False,
        )
    return AgentBoardEntry(
        name=AgentName(name),
        state=state,
        provider_name=ProviderInstanceName("local"),
        work_dir=work_dir,
        pr=pr,
        is_muted=is_muted,
    )


def _make_state(entries: tuple[AgentBoardEntry, ...] = ()) -> _KanpanState:
    """Create a minimal _KanpanState for testing pure functions."""
    snapshot = BoardSnapshot(entries=entries, fetch_time_seconds=0.1) if entries else None
    frame = _stub()
    footer_left_text = _stub()
    footer_left_attr = _stub()
    footer_right = _stub()
    # Use model_construct to bypass MngContext validation (we don't need a real one)
    state = _KanpanState.model_construct(
        mng_ctx=_stub(),
        frame=frame,
        footer_left_text=footer_left_text,
        footer_left_attr=footer_left_attr,
        footer_right=footer_right,
        snapshot=snapshot,
        spinner_index=0,
        marks={},
        pending_confirm_deletes=(),
        executing=False,
        execute_status="",
        index_to_entry={},
        list_walker=None,
        focused_agent_name=None,
        steady_footer_text="  Loading...",
        commands={},
        loop=None,
        refresh_future=None,
        executor=None,
    )
    if snapshot is not None:
        walker, state.index_to_entry = _build_board_widgets(snapshot)
        state.list_walker = walker
        frame.body = _stub()
    return state


def _text_from_widget(widget: Text) -> str:
    """Extract plain text content from a single Text widget."""
    raw = widget.text
    if isinstance(raw, str):
        return raw
    parts: list[str] = []
    for seg in raw:
        if isinstance(seg, tuple):
            parts.append(str(seg[1]))
        else:
            parts.append(str(seg))
    return "".join(parts)


def _extract_text(walker: list[object]) -> list[str]:
    """Extract plain text from all Text and Columns widgets in a walker."""
    texts: list[str] = []
    for widget in walker:
        inner = widget.original_widget if isinstance(widget, AttrMap) else widget
        if isinstance(inner, Text):
            texts.append(_text_from_widget(inner))
        elif isinstance(inner, Columns):
            # Columns contains (options, widget) pairs in .contents
            cell_texts = [_text_from_widget(child) for child, _options in inner.contents if isinstance(child, Text)]
            texts.append(" ".join(cell_texts))
    return texts


def _text_contains(texts: list[str], substring: str) -> bool:
    return any(substring in t for t in texts)


# --- _classify_entry ---


def test_classify_entry_no_pr() -> None:
    entry = _make_entry()
    assert _classify_entry(entry) == BoardSection.STILL_COOKING


def test_classify_entry_merged_pr() -> None:
    entry = _make_entry(pr_state=PrState.MERGED)
    assert _classify_entry(entry) == BoardSection.PR_MERGED


def test_classify_entry_closed_pr() -> None:
    entry = _make_entry(pr_state=PrState.CLOSED)
    assert _classify_entry(entry) == BoardSection.PR_CLOSED


def test_classify_entry_open_pr() -> None:
    entry = _make_entry(pr_state=PrState.OPEN)
    assert _classify_entry(entry) == BoardSection.PR_BEING_REVIEWED


def test_classify_entry_muted_overrides_pr_state() -> None:
    entry = _make_entry(pr_state=PrState.OPEN, is_muted=True)
    assert _classify_entry(entry) == BoardSection.MUTED


# --- _is_safe_to_delete ---


def test_is_safe_to_delete_merged_pr() -> None:
    entry = _make_entry(pr_state=PrState.MERGED)
    assert _is_safe_to_delete(entry) is True


def test_is_safe_to_delete_open_pr() -> None:
    entry = _make_entry(pr_state=PrState.OPEN)
    assert _is_safe_to_delete(entry) is False


def test_is_safe_to_delete_no_pr() -> None:
    entry = _make_entry()
    assert _is_safe_to_delete(entry) is False


# --- _get_name_cell_markup with marks ---


def test_name_cell_markup_no_mark() -> None:
    entry = _make_entry()
    result = _get_name_cell_markup(entry)
    assert isinstance(result, str)
    assert result.startswith("  agent-1")


def test_name_cell_markup_delete_mark() -> None:
    entry = _make_entry()
    result = _get_name_cell_markup(entry, mark=PendingMark.DELETE)
    assert isinstance(result, list)
    assert result[0] == ("mark_delete", "D")
    assert "agent-1" in result[1]


def test_name_cell_markup_push_mark() -> None:
    entry = _make_entry(work_dir=Path("/tmp/work"))
    result = _get_name_cell_markup(entry, mark=PendingMark.PUSH)
    assert isinstance(result, list)
    assert result[0] == ("mark_push", "P")


# --- _toggle_mark ---


def test_toggle_mark_adds_mark() -> None:
    entry = _make_entry()
    state = _make_state(entries=(entry,))
    # Focus the first selectable entry
    first_idx = min(state.index_to_entry.keys())
    state.list_walker.set_focus(first_idx)

    _toggle_mark(state, PendingMark.DELETE)
    assert state.marks == {AgentName("agent-1"): PendingMark.DELETE}


def test_toggle_mark_removes_same_mark() -> None:
    entry = _make_entry()
    state = _make_state(entries=(entry,))
    first_idx = min(state.index_to_entry.keys())
    state.list_walker.set_focus(first_idx)

    state.marks[AgentName("agent-1")] = PendingMark.DELETE
    _toggle_mark(state, PendingMark.DELETE)
    assert AgentName("agent-1") not in state.marks


def test_toggle_mark_replaces_different_mark() -> None:
    entry = _make_entry(work_dir=Path("/tmp/work"))
    state = _make_state(entries=(entry,))
    first_idx = min(state.index_to_entry.keys())
    state.list_walker.set_focus(first_idx)

    state.marks[AgentName("agent-1")] = PendingMark.DELETE
    _toggle_mark(state, PendingMark.PUSH)
    assert state.marks[AgentName("agent-1")] == PendingMark.PUSH


def test_toggle_push_mark_rejected_without_work_dir() -> None:
    entry = _make_entry()
    state = _make_state(entries=(entry,))
    first_idx = min(state.index_to_entry.keys())
    state.list_walker.set_focus(first_idx)

    _toggle_mark(state, PendingMark.PUSH)
    assert AgentName("agent-1") not in state.marks


# --- _unmark_focused ---


def test_unmark_focused_removes_mark() -> None:
    entry = _make_entry()
    state = _make_state(entries=(entry,))
    first_idx = min(state.index_to_entry.keys())
    state.list_walker.set_focus(first_idx)
    state.marks[AgentName("agent-1")] = PendingMark.DELETE

    _unmark_focused(state)
    assert AgentName("agent-1") not in state.marks


def test_unmark_focused_noop_without_mark() -> None:
    entry = _make_entry()
    state = _make_state(entries=(entry,))
    first_idx = min(state.index_to_entry.keys())
    state.list_walker.set_focus(first_idx)

    _unmark_focused(state)
    assert state.marks == {}


# --- _unmark_all ---


def test_unmark_all_clears_all_marks() -> None:
    e1 = _make_entry(name="agent-1")
    e2 = _make_entry(name="agent-2")
    state = _make_state(entries=(e1, e2))
    state.marks[AgentName("agent-1")] = PendingMark.DELETE
    state.marks[AgentName("agent-2")] = PendingMark.PUSH

    _unmark_all(state)
    assert state.marks == {}


def test_unmark_all_noop_when_empty() -> None:
    state = _make_state(entries=(_make_entry(),))
    _unmark_all(state)
    assert state.marks == {}


# --- _advance_focus ---


def test_advance_focus_moves_to_next_agent() -> None:
    e1 = _make_entry(name="agent-1")
    e2 = _make_entry(name="agent-2")
    state = _make_state(entries=(e1, e2))

    sorted_indices = sorted(state.index_to_entry.keys())
    state.list_walker.set_focus(sorted_indices[0])

    _advance_focus(state)

    _, new_focus = state.list_walker.get_focus()
    assert new_focus == sorted_indices[1]


def test_advance_focus_stays_at_last_agent() -> None:
    entry = _make_entry()
    state = _make_state(entries=(entry,))
    first_idx = min(state.index_to_entry.keys())
    state.list_walker.set_focus(first_idx)

    _advance_focus(state)

    _, new_focus = state.list_walker.get_focus()
    assert new_focus == first_idx


# --- _build_board_widgets with marks ---


def test_build_board_widgets_shows_mark_indicator() -> None:
    entry = _make_entry()
    snapshot = BoardSnapshot(entries=(entry,), fetch_time_seconds=0.1)
    marks = {AgentName("agent-1"): PendingMark.DELETE}

    walker, index_to_entry = _build_board_widgets(snapshot, marks)

    agent_idx = min(index_to_entry.keys())
    texts = _extract_text([walker[agent_idx]])
    assert len(texts) == 1
    assert texts[0].startswith("D")


# === _carry_forward_pr_data ===


def test_carry_forward_pr_data_preserves_old_prs() -> None:
    pr = make_pr_info(number=42, head_branch="mng/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=pr,
        create_pr_url=None,
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh auth failed",),
        prs_loaded=False,
        fetch_time_seconds=2.0,
    )

    result = _carry_forward_pr_data(old, new)
    assert result.prs_loaded is True
    assert result.entries[0].pr is not None
    assert result.entries[0].pr.number == 42
    # Errors from the failed fetch are still preserved
    assert "gh auth failed" in result.errors[0]
    # Timing comes from the new snapshot
    assert result.fetch_time_seconds == 2.0


def test_carry_forward_pr_data_preserves_create_pr_url_without_pr() -> None:
    """When the old snapshot has a create_pr_url but no PR, it should be carried forward."""
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url="https://github.com/org/repo/compare/mng/agent-1?expand=1",
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(entries=(new_entry,), prs_loaded=False, fetch_time_seconds=2.0)

    result = _carry_forward_pr_data(old, new)
    assert result.prs_loaded is True
    assert result.entries[0].pr is None
    assert result.entries[0].create_pr_url == "https://github.com/org/repo/compare/mng/agent-1?expand=1"


def test_carry_forward_pr_data_handles_new_agents() -> None:
    """New agents that weren't in the old snapshot get no PR data carried forward."""
    old = BoardSnapshot(entries=(), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-new"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-new",
    )
    new = BoardSnapshot(entries=(new_entry,), prs_loaded=False, fetch_time_seconds=2.0)

    result = _carry_forward_pr_data(old, new)
    assert result.entries[0].pr is None


# === _build_board_widgets: first-load PR failure ===


def test_first_load_pr_failure_shows_prs_not_loaded() -> None:
    """When the first load fails to fetch PRs, the heading should say 'PRs not loaded'
    and no create-PR links should appear."""
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        errors=("gh pr list failed: auth required",),
        prs_loaded=False,
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "PRs not loaded")
    assert not _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "create PR")
    assert _text_contains(texts, "gh pr list failed")


def test_first_load_pr_success_shows_normal_heading() -> None:
    """When PRs load successfully, agents without PRs show normal 'no PR yet' heading."""
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url="https://github.com/org/repo/compare/mng/agent-1?expand=1",
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        prs_loaded=True,
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "PRs not loaded")


def test_second_load_pr_failure_shows_carried_forward_prs() -> None:
    """When the second load fails to fetch PRs, carry-forward preserves PR data
    and the TUI shows normal PR info (not 'PRs not loaded')."""
    pr = make_pr_info(number=42, head_branch="mng/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=pr,
        create_pr_url=None,
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh pr list failed: network error",),
        prs_loaded=False,
        fetch_time_seconds=2.0,
    )

    carried = _carry_forward_pr_data(old, new)
    walker, _ = _build_board_widgets(carried)

    texts = _extract_text(list(walker))
    # Carried-forward PR data renders the same as a normal successful load
    assert _text_contains(texts, "github.com/org/repo/pull/42")
    assert not _text_contains(texts, "PRs not loaded")
    assert not _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "create PR")
    # Error from the failed fetch is still visible
    assert _text_contains(texts, "network error")
