import re
from enum import Enum
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.ratchets import check_no_ruff_errors
from imbue.imbue_common.ratchet_testing.ratchets import check_no_type_errors
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import IdleMode

# mngr's test_ratchets.py is nested one level deeper than other projects (in utils/),
# so the source dir is parent.parent instead of parent.parent.parent
_DIR = Path(__file__).parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(2))


def test_prevent_exec() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    rc.check_time_sleep(_DIR, snapshot(1))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(34), excluded_patterns=("_kqueue_tty_test_script.py",))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    rc.check_broad_exception_catch(_DIR, snapshot(9))


def test_prevent_base_exception_catch() -> None:
    rc.check_base_exception_catch(_DIR, snapshot(1))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


def test_prevent_silent_decode_error_catches() -> None:
    rc.check_silent_decode_error_catches(_DIR, snapshot(12))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(3))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


def test_prevent_import_datetime() -> None:
    rc.check_import_datetime(_DIR, snapshot(0))


def test_prevent_importlib_import_module() -> None:
    rc.check_importlib_import_module(_DIR, snapshot(0))


def test_prevent_getattr() -> None:
    rc.check_getattr(_DIR, snapshot(9))


def test_prevent_setattr() -> None:
    rc.check_setattr(_DIR, snapshot(1))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    rc.check_asyncio_import(_DIR, snapshot(0))


def test_prevent_pandas_import() -> None:
    rc.check_pandas_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))


def test_prevent_namedtuple() -> None:
    rc.check_namedtuple(_DIR, snapshot(6))


def test_prevent_yaml_usage() -> None:
    rc.check_yaml_usage(_DIR, snapshot(0))


def test_prevent_functools_partial() -> None:
    rc.check_functools_partial(_DIR, snapshot(0))


def test_prevent_exit_stack() -> None:
    rc.check_exit_stack(_DIR, snapshot(5))


# --- Hardcoded paths ---


def test_prevent_hardcoded_claude_dir() -> None:
    rc.check_hardcoded_claude_dir(_DIR, snapshot(0))


# The non-zero count covers the session-scoped dockerd-startup fixture in conftest.py,
# which is autouse and fires for tests without @pytest.mark.docker, so it must bypass
# the PATH wrapper (which would otherwise block the docker invocation).
def test_prevent_hardcoded_guarded_binary() -> None:
    rc.check_hardcoded_guarded_binary(_DIR, snapshot(2))


# --- Naming conventions ---


def test_prevent_num_prefix() -> None:
    rc.check_num_prefix(_DIR, snapshot(0))


# --- Documentation ---


def test_prevent_trailing_comments() -> None:
    rc.check_trailing_comments(_DIR, snapshot(0))


def test_prevent_init_docstrings() -> None:
    rc.check_init_docstrings(_DIR, snapshot(0))


@pytest.mark.timeout(10)
def test_prevent_args_in_docstrings() -> None:
    rc.check_args_in_docstrings(_DIR, snapshot(0))


@pytest.mark.timeout(10)
def test_prevent_returns_in_docstrings() -> None:
    rc.check_returns_in_docstrings(_DIR, snapshot(0))


# --- Type safety ---


def test_prevent_literal_with_multiple_options() -> None:
    rc.check_literal_with_multiple_options(_DIR, snapshot(0))


def test_prevent_bare_generic_types() -> None:
    rc.check_bare_generic_types(_DIR, snapshot(0))


def test_prevent_typing_builtin_imports() -> None:
    rc.check_typing_builtin_imports(_DIR, snapshot(0))


def test_prevent_short_uuid_ids() -> None:
    rc.check_short_uuid_ids(_DIR, snapshot(2))


# --- Pydantic / models ---


def test_prevent_model_copy() -> None:
    rc.check_model_copy(_DIR, snapshot(0))


# --- Logging ---


def test_prevent_fstring_logging() -> None:
    rc.check_fstring_logging(_DIR, snapshot(0))


def test_prevent_click_echo() -> None:
    rc.check_click_echo(_DIR, snapshot(0))


def test_prevent_logger_exception() -> None:
    rc.check_logger_exception(_DIR, snapshot(0))


# --- Testing conventions ---


def test_prevent_unittest_mock_imports() -> None:
    rc.check_unittest_mock_imports(_DIR, snapshot(3))


def test_prevent_monkeypatch_setattr() -> None:
    rc.check_monkeypatch_setattr(_DIR, snapshot(35))


def test_prevent_test_container_classes() -> None:
    rc.check_test_container_classes(_DIR, snapshot(0))


def test_prevent_pytest_mark_integration() -> None:
    rc.check_pytest_mark_integration(_DIR, snapshot(0))


# --- Process management ---


def test_prevent_os_fork() -> None:
    rc.check_os_fork(_DIR, snapshot(2))


def test_prevent_bare_urwid_tty_signal_keys() -> None:
    rc.check_bare_urwid_tty_signal_keys(_DIR, snapshot(0))


def test_prevent_direct_subprocess() -> None:
    # testing.py files are test infrastructure and excluded alongside test files
    excluded = TEST_FILE_PATTERNS + ("testing.py",)
    rc.check_direct_subprocess(_DIR, snapshot(20), excluded_patterns=excluded)


# --- AST-based ratchets ---


def test_prevent_if_elif_without_else() -> None:
    rc.check_if_elif_without_else(_DIR, snapshot(0))


def test_prevent_inline_functions() -> None:
    rc.check_inline_functions(_DIR, snapshot(0))


def test_prevent_underscore_imports() -> None:
    rc.check_underscore_imports(_DIR, snapshot(0))


def test_prevent_init_methods_in_non_exception_classes() -> None:
    rc.check_init_methods_in_non_exception_classes(_DIR, snapshot(3))


def test_prevent_cast_usage() -> None:
    rc.check_cast_usage(_DIR, snapshot(9))


def test_prevent_assert_isinstance() -> None:
    rc.check_assert_isinstance(_DIR, snapshot(0))


# --- Project-level checks ---


def test_prevent_code_in_init_files() -> None:
    rc.check_code_in_init_files(
        _DIR,
        snapshot(0),
        allowed_root_init_lines={
            "import pluggy",
            'hookimpl = pluggy.HookimplMarker("mngr")',
        },
    )


def test_no_type_errors() -> None:
    """Ensure the codebase has zero type errors."""
    check_no_type_errors(Path(__file__).parent.parent.parent.parent)


def test_no_ruff_errors() -> None:
    """Ensure the codebase has zero ruff linting errors."""
    check_no_ruff_errors(Path(__file__).parent.parent.parent.parent)


# --- Doc-vs-code invariants ---

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
