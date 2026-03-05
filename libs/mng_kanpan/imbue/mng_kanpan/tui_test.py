from urwid.widget.attr_map import AttrMap
from urwid.widget.text import Text

from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.testing import make_pr_info
from imbue.mng_kanpan.tui import _build_board_widgets
from imbue.mng_kanpan.tui import _carry_forward_pr_data


def _extract_text(walker: list[object]) -> list[str]:
    """Extract plain text from all Text widgets in a walker."""
    texts: list[str] = []
    for widget in walker:
        inner = widget.original_widget if isinstance(widget, AttrMap) else widget
        if not isinstance(inner, Text):
            continue
        raw = inner.text
        if isinstance(raw, str):
            texts.append(raw)
        else:
            parts: list[str] = []
            for seg in raw:
                if isinstance(seg, tuple):
                    parts.append(str(seg[1]))
                else:
                    parts.append(str(seg))
            texts.append("".join(parts))
    return texts


def _text_contains(texts: list[str], substring: str) -> bool:
    return any(substring in t for t in texts)


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
