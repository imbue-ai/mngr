import re
from enum import Enum
from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import IdleMode

# The mngr project root (libs/mngr/), used to resolve docs paths.
# parent chain: utils/ -> imbue/mngr/ -> imbue/ -> libs/mngr/
_MNGR_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

# Pairs of (enum class, docs file path relative to the mngr project root). The
# docs file is expected to contain exactly one markdown table whose first column
# lists every member of the enum, one row per member. Other tables on the same
# page (e.g. unrelated reference tables) are ignored as long as their first
# column does not contain any of the enum's member names.
ENUM_DOCS_TABLE_PAIRS: tuple[tuple[type[Enum], str], ...] = (
    (HostState, "docs/concepts/hosts.md"),
    (AgentLifecycleState, "docs/concepts/agents.md"),
    (IdleMode, "docs/concepts/idle_detection.md"),
)

# Strips backticks and asterisks (bold/italic) from a markdown table cell so we
# can recover the raw identifier underneath the formatting.
_MARKDOWN_FORMATTING = re.compile(r"[`*]+")

# Matches an identifier (letters, digits, underscores) — the shape used for
# enum member names in the markdown tables (e.g. `running_unknown_agent_type`).
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _parse_markdown_tables(markdown_text: str) -> list[list[str]]:
    """Return one entry per markdown table in the text — the list of first-column labels for that table (uppercased).

    A markdown table is a contiguous block of lines starting with ``|``. We
    detect the separator row (``|---|---|``) to know where the header ends; data
    rows are everything after that. Each data row's first cell is stripped of
    formatting (``**``, backticks) and reduced to its leading identifier.
    """
    tables: list[list[str]] = []
    current: list[str] = []
    in_data_rows = False
    header_pending = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            if current:
                tables.append(current)
                current = []
            in_data_rows = False
            header_pending = False
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        if all(re.fullmatch(r"[:\- ]+", c) for c in cells):
            # Separator row — header is above us, data rows follow.
            in_data_rows = True
            header_pending = False
            continue
        if not in_data_rows:
            # Header row of a new table.
            header_pending = True
            continue
        if header_pending:
            continue
        first_cell = _MARKDOWN_FORMATTING.sub("", cells[0])
        match = _IDENTIFIER.search(first_cell)
        if match is not None:
            current.append(match.group(0).upper())
    if current:
        tables.append(current)
    return tables


@pytest.mark.parametrize(
    ("enum_cls", "docs_relpath"),
    ENUM_DOCS_TABLE_PAIRS,
    ids=[f"{cls.__name__}-{path}" for cls, path in ENUM_DOCS_TABLE_PAIRS],
)
def test_enum_matches_docs_table(enum_cls: type[Enum], docs_relpath: str) -> None:
    """Every enum member must appear as a row in its docs table, and vice versa.

    See ``style_guide.md`` ("Programmatic enforcement of doc-vs-code invariants")
    for the rationale: docs that enumerate enum members rot silently when the
    code changes and reviewers don't notice, so we enforce the invariant in CI.
    """
    docs_path = _MNGR_PROJECT_ROOT / docs_relpath
    docs_text = docs_path.read_text()
    tables = _parse_markdown_tables(docs_text)
    enum_labels = frozenset(enum_cls.__members__)
    # Identify the enum's table: the one whose first-column labels overlap with
    # this enum's members. (Other tables on the same page are unrelated.)
    candidate_tables = [t for t in tables if enum_labels & frozenset(t)]
    assert len(candidate_tables) == 1, (
        f"Expected exactly one markdown table in {docs_relpath} listing "
        f"{enum_cls.__name__} members; found {len(candidate_tables)}."
    )
    docs_labels = frozenset(candidate_tables[0])
    missing_from_docs = enum_labels - docs_labels
    extra_in_docs = docs_labels - enum_labels
    assert not missing_from_docs, (
        f"Enum members of {enum_cls.__name__} are missing from {docs_relpath}: "
        f"{sorted(missing_from_docs)}. Add a row to the markdown table whose "
        f"first cell contains the (case-insensitive) member name."
    )
    assert not extra_in_docs, (
        f"{docs_relpath} lists rows that are not members of "
        f"{enum_cls.__name__}: {sorted(extra_in_docs)}. Either add the missing "
        f"enum member or remove the stale docs row."
    )
