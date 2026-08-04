"""Unit tests for the kanpan header status template and its counts."""

from datetime import datetime
from datetime import timezone
from typing import Any

import pytest

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_sources.github import PrFetchFailedField
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.header_status import KanpanHeaderStatusError
from imbue.mngr_kanpan.header_status import compile_header_status
from imbue.mngr_kanpan.header_status import render_header_status
from imbue.mngr_kanpan.testing import make_board_entry
from imbue.mngr_kanpan.testing import make_board_snapshot
from imbue.mngr_kanpan.testing import make_pr_field

_RUNNING = '{state == "RUNNING"}'
_ALL_SECTIONS: tuple[BoardSection, ...] = tuple(BoardSection)


# =============================================================================
# Compilation
# =============================================================================


def test_compile_unset_template_is_none() -> None:
    assert compile_header_status(None) is None


def test_compile_template_without_counts() -> None:
    status = compile_header_status("no counts here")
    assert status is not None
    assert len(status.segments) == 1


@pytest.mark.parametrize(
    ("template", "expected_message"),
    [
        pytest.param("{total", "never closed", id="unclosed-brace"),
        pytest.param('{state == "RUNNING"} }', "closes nothing", id="unmatched-close-brace"),
        pytest.param("{} agents", "empty", id="empty-expression"),
        pytest.param("{  } agents", "empty", id="whitespace-only-expression"),
        pytest.param("{state ===} agents", "not valid CEL", id="invalid-cel"),
    ],
)
def test_compile_rejects_a_misconfigured_header_status(template: str, expected_message: str) -> None:
    with pytest.raises(KanpanHeaderStatusError, match=expected_message):
        compile_header_status(template)


def test_compile_rejects_config_whose_type_the_loader_did_not_check() -> None:
    # The mngr config loader builds plugin configs with `model_construct`, which
    # bypasses Pydantic validation and leaves the field holding whatever the user's
    # TOML did, so a non-string reaches compile_header_status through a real config.
    config = KanpanPluginConfig.model_construct(header_status=True)
    header_status: Any = config.header_status
    with pytest.raises(KanpanHeaderStatusError, match="header_status must be a string"):
        compile_header_status(header_status)


# =============================================================================
# Rendering
# =============================================================================


def test_render_unconfigured_is_empty() -> None:
    snapshot = make_board_snapshot(entries=(make_board_entry(),))
    assert render_header_status(None, snapshot, _ALL_SECTIONS) == ""


def test_render_before_first_fetch_is_empty() -> None:
    status = compile_header_status("{total} agents")
    assert render_header_status(status, None, _ALL_SECTIONS) == ""


def test_render_total_counts_every_entry() -> None:
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a"),
            make_board_entry(name="b", state=AgentLifecycleState.STOPPED),
        )
    )
    status = compile_header_status("{total} agents")
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "2 agents"


def test_render_empty_board_counts_zero() -> None:
    status = compile_header_status(f"{_RUNNING} running / {{total}}")
    assert render_header_status(status, make_board_snapshot(), _ALL_SECTIONS) == "0 running / 0"


def test_render_counts_by_state() -> None:
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", state=AgentLifecycleState.RUNNING),
            make_board_entry(name="b", state=AgentLifecycleState.RUNNING),
            make_board_entry(name="c", state=AgentLifecycleState.STOPPED),
        )
    )
    status = compile_header_status(f"{_RUNNING} running / {{total}}")
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "2 running / 3"


def test_render_counts_by_section() -> None:
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", section=BoardSection.PR_MERGED),
            make_board_entry(name="b", section=BoardSection.STILL_COOKING),
        )
    )
    status = compile_header_status('{section == "PR_MERGED"} merged')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1 merged"


def test_render_counts_by_cell_text() -> None:
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", cells={"ci": CellDisplay(text="failure")}),
            make_board_entry(name="b", cells={"ci": CellDisplay(text="success")}),
        )
    )
    status = compile_header_status('{cells.ci.text == "failure"} failing')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1 failing"


def test_render_counts_by_structured_field() -> None:
    pr_field = make_pr_field(created=datetime.now(tz=timezone.utc), number=42)
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", fields={"pr": pr_field}),
            make_board_entry(name="b"),
        )
    )
    status = compile_header_status('{fields.pr.state == "OPEN"} open')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1 open"


def test_render_ignores_entries_missing_the_counted_column() -> None:
    """An agent with no value for the counted column is simply not counted."""
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", cells={"ci": CellDisplay(text="failure")}),
            make_board_entry(name="b"),
        )
    )
    status = compile_header_status('{cells.ci.text == "failure"}/{total}')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1/2"


def test_render_ignores_entries_whose_column_payload_lacks_the_member(log_warnings: list[str]) -> None:
    """A column whose payload has no such member is not counted, and does not warn.

    The `pr` column holds a fetch-failure sentinel as readily as a pull request, and
    the board owns the terminal while counts run, so a warning would land on it.
    """
    now = datetime.now(tz=timezone.utc)
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", fields={"pr": make_pr_field(created=now)}),
            make_board_entry(name="b", fields={"pr": PrFetchFailedField(created=now, repo="org/repo")}),
        )
    )
    status = compile_header_status('{fields.pr.state == "OPEN"}/{total}')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1/2"
    assert log_warnings == []


def test_render_leaves_a_count_outside_the_entry_shape_at_zero(log_warnings: list[str]) -> None:
    """A count naming what the board entry does not carry counts nothing, naming the count."""
    snapshot = make_board_snapshot(entries=(make_board_entry(),))
    status = compile_header_status('{labels.project == "mngr"}/{total}')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "0/1"
    assert [message for message in log_warnings if "labels.project" in message] != []


def test_render_ignores_entries_in_sections_the_board_omits() -> None:
    """Counts follow a customized section order, as the board and `--format json` do."""
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", section=BoardSection.STILL_COOKING),
            make_board_entry(name="b", section=BoardSection.MUTED),
        )
    )
    status = compile_header_status(f"{_RUNNING} running / {{total}}")
    assert render_header_status(status, snapshot, (BoardSection.STILL_COOKING,)) == "1 running / 1"


def test_render_several_counts() -> None:
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", state=AgentLifecycleState.RUNNING, cells={"ci": CellDisplay(text="failure")}),
            make_board_entry(name="b", state=AgentLifecycleState.STOPPED),
        )
    )
    status = compile_header_status(f'{_RUNNING} running · {{cells.ci.text == "failure"}} red / {{total}}')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1 running · 1 red / 2"


def test_render_the_same_expression_twice() -> None:
    """Each brace is counted where it stands, so repeating one is not a name collision."""
    snapshot = make_board_snapshot(entries=(make_board_entry(),))
    status = compile_header_status("{total} of {total}")
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1 of 1"


def test_render_literal_braces() -> None:
    snapshot = make_board_snapshot(entries=(make_board_entry(),))
    status = compile_header_status("{{{total}}}")
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "{1}"


def test_render_counts_an_expression_holding_a_brace_in_a_string() -> None:
    """A brace inside a CEL string literal does not end the expression."""
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", cells={"ci": CellDisplay(text="}")}),
            make_board_entry(name="b", cells={"ci": CellDisplay(text="success")}),
        )
    )
    status = compile_header_status('{cells.ci.text == "}"} odd')
    assert render_header_status(status, snapshot, _ALL_SECTIONS) == "1 odd"
