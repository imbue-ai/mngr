from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_ARGS_IN_DOCSTRINGS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_ASSERT_ISINSTANCE
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_BARE_EXCEPT
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_BARE_GENERIC_TYPES
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_BARE_PRINT
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_BASE_EXCEPTION_CATCH
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_BROAD_EXCEPTION_CATCH
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_BUILTIN_EXCEPTION_RAISES
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_CAST_USAGE
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_CLICK_ECHO
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_DATACLASSES_IMPORT
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_DIRECT_SUBPROCESS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_EVAL
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_EXEC
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_FSTRING_LOGGING
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_FUNCTOOLS_PARTIAL
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_GETATTR
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_GLOBAL_KEYWORD
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_IF_ELIF_WITHOUT_ELSE
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_IMPORTLIB_IMPORT_MODULE
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_IMPORT_DATETIME
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_INIT_DOCSTRINGS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_INIT_IN_NON_EXCEPTION_CLASSES
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_INLINE_FUNCTIONS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_INLINE_IMPORTS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_LITERAL_MULTIPLE_OPTIONS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_MODEL_COPY
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_MONKEYPATCH_SETATTR
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_NAMEDTUPLE
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_NUM_PREFIX
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_OS_FORK
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_PANDAS_IMPORT
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_PYTEST_MARK_INTEGRATION
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_RELATIVE_IMPORTS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_RETURNS_IN_DOCSTRINGS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_SETATTR
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_SHORT_UUID_IDS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TEST_CONTAINER_CLASSES
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TIME_SLEEP
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TODOS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TRAILING_COMMENTS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TYPING_BUILTIN_IMPORTS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_UNDERSCORE_IMPORTS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_UNITTEST_MOCK_IMPORTS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_WHILE_TRUE
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_YAML_USAGE
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule
from imbue.imbue_common.ratchet_testing.core import clear_ratchet_caches
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.ratchets import _is_test_file
from imbue.imbue_common.ratchet_testing.ratchets import check_no_ruff_errors
from imbue.imbue_common.ratchet_testing.ratchets import check_no_type_errors
from imbue.imbue_common.ratchet_testing.ratchets import find_assert_isinstance_usages
from imbue.imbue_common.ratchet_testing.ratchets import find_bash_scripts_without_strict_mode
from imbue.imbue_common.ratchet_testing.ratchets import find_cast_usages
from imbue.imbue_common.ratchet_testing.ratchets import find_code_in_init_files
from imbue.imbue_common.ratchet_testing.ratchets import find_if_elif_without_else
from imbue.imbue_common.ratchet_testing.ratchets import find_init_methods_in_non_exception_classes
from imbue.imbue_common.ratchet_testing.ratchets import find_inline_functions
from imbue.imbue_common.ratchet_testing.ratchets import find_underscore_imports

# Exclude this test file from ratchet scans to prevent self-referential matches
_SELF_EXCLUSION: tuple[str, ...] = ("test_ratchets.py",)

# Group all ratchet tests onto a single xdist worker to benefit from LRU caching
pytestmark = pytest.mark.xdist_group(name="ratchets")


def teardown_module() -> None:
    """Clear ratchet LRU caches after all tests in this module complete."""
    clear_ratchet_caches()


def _get_changelings_source_dir() -> Path:
    return Path(__file__).parent.parent.parent


# --- Code safety ---


def test_prevent_todos() -> None:
    chunks = check_ratchet_rule(PREVENT_TODOS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_TODOS.format_failure(chunks)


def test_prevent_exec_usage() -> None:
    chunks = check_ratchet_rule(PREVENT_EXEC, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_EXEC.format_failure(chunks)


def test_prevent_eval_usage() -> None:
    chunks = check_ratchet_rule(PREVENT_EVAL, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_EVAL.format_failure(chunks)


def test_prevent_while_true() -> None:
    chunks = check_ratchet_rule(PREVENT_WHILE_TRUE, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(1), PREVENT_WHILE_TRUE.format_failure(chunks)


def test_prevent_time_sleep() -> None:
    chunks = check_ratchet_rule(PREVENT_TIME_SLEEP, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_TIME_SLEEP.format_failure(chunks)


def test_prevent_global_keyword() -> None:
    chunks = check_ratchet_rule(PREVENT_GLOBAL_KEYWORD, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_GLOBAL_KEYWORD.format_failure(chunks)


def test_prevent_bare_print() -> None:
    chunks = check_ratchet_rule(PREVENT_BARE_PRINT, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_BARE_PRINT.format_failure(chunks)


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    chunks = check_ratchet_rule(PREVENT_BARE_EXCEPT, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_BARE_EXCEPT.format_failure(chunks)


def test_prevent_broad_exception_catch() -> None:
    chunks = check_ratchet_rule(PREVENT_BROAD_EXCEPTION_CATCH, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_BROAD_EXCEPTION_CATCH.format_failure(chunks)


def test_prevent_base_exception_catch() -> None:
    chunks = check_ratchet_rule(PREVENT_BASE_EXCEPTION_CATCH, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_BASE_EXCEPTION_CATCH.format_failure(chunks)


def test_prevent_builtin_exception_raises() -> None:
    chunks = check_ratchet_rule(PREVENT_BUILTIN_EXCEPTION_RAISES, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_BUILTIN_EXCEPTION_RAISES.format_failure(chunks)


# --- Import style ---


def test_prevent_inline_imports() -> None:
    chunks = check_ratchet_rule(PREVENT_INLINE_IMPORTS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_INLINE_IMPORTS.format_failure(chunks)


def test_prevent_relative_imports() -> None:
    chunks = check_ratchet_rule(PREVENT_RELATIVE_IMPORTS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_RELATIVE_IMPORTS.format_failure(chunks)


def test_prevent_import_datetime() -> None:
    chunks = check_ratchet_rule(PREVENT_IMPORT_DATETIME, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_IMPORT_DATETIME.format_failure(chunks)


def test_prevent_importlib_import_module() -> None:
    chunks = check_ratchet_rule(PREVENT_IMPORTLIB_IMPORT_MODULE, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_IMPORTLIB_IMPORT_MODULE.format_failure(chunks)


def test_prevent_getattr() -> None:
    chunks = check_ratchet_rule(PREVENT_GETATTR, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_GETATTR.format_failure(chunks)


def test_prevent_setattr() -> None:
    chunks = check_ratchet_rule(PREVENT_SETATTR, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_SETATTR.format_failure(chunks)


# --- Banned libraries and patterns ---


def test_prevent_pandas_import() -> None:
    chunks = check_ratchet_rule(PREVENT_PANDAS_IMPORT, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_PANDAS_IMPORT.format_failure(chunks)


def test_prevent_dataclasses_import() -> None:
    chunks = check_ratchet_rule(PREVENT_DATACLASSES_IMPORT, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_DATACLASSES_IMPORT.format_failure(chunks)


def test_prevent_namedtuple_usage() -> None:
    chunks = check_ratchet_rule(PREVENT_NAMEDTUPLE, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_NAMEDTUPLE.format_failure(chunks)


def test_prevent_yaml_usage() -> None:
    chunks = check_ratchet_rule(PREVENT_YAML_USAGE, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_YAML_USAGE.format_failure(chunks)


def test_prevent_functools_partial() -> None:
    chunks = check_ratchet_rule(PREVENT_FUNCTOOLS_PARTIAL, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_FUNCTOOLS_PARTIAL.format_failure(chunks)


# --- Naming conventions ---


def test_prevent_num_prefix() -> None:
    chunks = check_ratchet_rule(PREVENT_NUM_PREFIX, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_NUM_PREFIX.format_failure(chunks)


# --- Documentation ---


def test_prevent_trailing_comments() -> None:
    chunks = check_ratchet_rule(PREVENT_TRAILING_COMMENTS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_TRAILING_COMMENTS.format_failure(chunks)


def test_prevent_init_docstrings() -> None:
    chunks = check_ratchet_rule(PREVENT_INIT_DOCSTRINGS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_INIT_DOCSTRINGS.format_failure(chunks)


@pytest.mark.timeout(10)
def test_prevent_args_in_docstrings() -> None:
    chunks = check_ratchet_rule(PREVENT_ARGS_IN_DOCSTRINGS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_ARGS_IN_DOCSTRINGS.format_failure(chunks)


@pytest.mark.timeout(10)
def test_prevent_returns_in_docstrings() -> None:
    chunks = check_ratchet_rule(PREVENT_RETURNS_IN_DOCSTRINGS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_RETURNS_IN_DOCSTRINGS.format_failure(chunks)


# --- Type safety ---


def test_prevent_literal_with_multiple_options() -> None:
    chunks = check_ratchet_rule(PREVENT_LITERAL_MULTIPLE_OPTIONS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_LITERAL_MULTIPLE_OPTIONS.format_failure(chunks)


def test_prevent_bare_generic_types() -> None:
    chunks = check_ratchet_rule(PREVENT_BARE_GENERIC_TYPES, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_BARE_GENERIC_TYPES.format_failure(chunks)


def test_prevent_typing_builtin_imports() -> None:
    chunks = check_ratchet_rule(PREVENT_TYPING_BUILTIN_IMPORTS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_TYPING_BUILTIN_IMPORTS.format_failure(chunks)


def test_prevent_short_uuid_ids() -> None:
    chunks = check_ratchet_rule(PREVENT_SHORT_UUID_IDS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_SHORT_UUID_IDS.format_failure(chunks)


# --- Pydantic / models ---


def test_prevent_model_copy() -> None:
    chunks = check_ratchet_rule(PREVENT_MODEL_COPY, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_MODEL_COPY.format_failure(chunks)


# --- Logging ---


def test_prevent_fstring_logging() -> None:
    chunks = check_ratchet_rule(PREVENT_FSTRING_LOGGING, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_FSTRING_LOGGING.format_failure(chunks)


def test_prevent_click_echo() -> None:
    chunks = check_ratchet_rule(PREVENT_CLICK_ECHO, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_CLICK_ECHO.format_failure(chunks)


# --- Testing conventions ---


def test_prevent_unittest_mock_imports() -> None:
    chunks = check_ratchet_rule(PREVENT_UNITTEST_MOCK_IMPORTS, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_UNITTEST_MOCK_IMPORTS.format_failure(chunks)


def test_prevent_monkeypatch_setattr() -> None:
    chunks = check_ratchet_rule(PREVENT_MONKEYPATCH_SETATTR, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_MONKEYPATCH_SETATTR.format_failure(chunks)


def test_prevent_test_container_classes() -> None:
    all_chunks = check_ratchet_rule(PREVENT_TEST_CONTAINER_CLASSES, _get_changelings_source_dir(), _SELF_EXCLUSION)
    chunks = tuple(c for c in all_chunks if _is_test_file(c.file_path))
    assert len(chunks) <= snapshot(0), PREVENT_TEST_CONTAINER_CLASSES.format_failure(chunks)


def test_prevent_pytest_mark_integration() -> None:
    chunks = check_ratchet_rule(PREVENT_PYTEST_MARK_INTEGRATION, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_PYTEST_MARK_INTEGRATION.format_failure(chunks)


# --- Process management ---


def test_prevent_os_fork() -> None:
    """Prevent usage of os.fork and os.forkpty.

    Forking is incompatible with threading: a forked child inherits only the calling
    thread, leaving mutexes held by other threads permanently locked and shared state
    inconsistent. Code should use the subprocess module to launch subprocesses instead.
    """
    chunks = check_ratchet_rule(PREVENT_OS_FORK, _get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_OS_FORK.format_failure(chunks)


def test_prevent_direct_subprocess_usage() -> None:
    """Prevent direct usage of subprocess and os process-spawning functions.

    Test files are excluded from this check.
    """
    chunks = check_ratchet_rule(PREVENT_DIRECT_SUBPROCESS, _get_changelings_source_dir(), TEST_FILE_PATTERNS)
    assert len(chunks) <= snapshot(0), PREVENT_DIRECT_SUBPROCESS.format_failure(chunks)


# --- AST-based ratchets ---


def test_prevent_if_elif_without_else() -> None:
    chunks = find_if_elif_without_else(_get_changelings_source_dir(), _SELF_EXCLUSION)
    assert len(chunks) <= snapshot(0), PREVENT_IF_ELIF_WITHOUT_ELSE.format_failure(chunks)


def test_prevent_inline_functions_in_non_test_code() -> None:
    chunks = find_inline_functions(_get_changelings_source_dir())
    assert len(chunks) <= snapshot(0), PREVENT_INLINE_FUNCTIONS.format_failure(chunks)


def test_prevent_importing_underscore_prefixed_names_in_non_test_code() -> None:
    chunks = find_underscore_imports(_get_changelings_source_dir())
    assert len(chunks) <= snapshot(0), PREVENT_UNDERSCORE_IMPORTS.format_failure(chunks)


def test_prevent_init_methods_in_non_exception_classes() -> None:
    chunks = find_init_methods_in_non_exception_classes(_get_changelings_source_dir())
    assert len(chunks) <= snapshot(0), PREVENT_INIT_IN_NON_EXCEPTION_CLASSES.format_failure(chunks)


def test_prevent_cast_usage() -> None:
    chunks = find_cast_usages(_get_changelings_source_dir())
    assert len(chunks) <= snapshot(0), PREVENT_CAST_USAGE.format_failure(chunks)


def test_prevent_assert_isinstance_usage() -> None:
    chunks = find_assert_isinstance_usages(_get_changelings_source_dir())
    assert len(chunks) <= snapshot(0), PREVENT_ASSERT_ISINSTANCE.format_failure(chunks)


# --- Project-specific ratchets ---


def test_prevent_code_in_init_files() -> None:
    """Ensure all __init__.py files are empty (per style guide)."""
    violations = find_code_in_init_files(_get_changelings_source_dir())
    assert len(violations) <= snapshot(0), (
        "Code found in __init__.py files (should be empty per style guide):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_no_type_errors() -> None:
    """Ensure the codebase has zero type errors."""
    check_no_type_errors(Path(__file__).parent.parent.parent)


def test_no_ruff_errors() -> None:
    """Ensure the codebase has zero ruff linting errors."""
    check_no_ruff_errors(Path(__file__).parent.parent.parent)


def test_prevent_bash_without_strict_mode() -> None:
    """Ensure all bash scripts use 'set -euo pipefail' for strict error handling."""
    violations = find_bash_scripts_without_strict_mode(Path(__file__).parent)
    assert len(violations) <= snapshot(0), "Bash scripts missing 'set -euo pipefail':\n" + "\n".join(
        f"  - {v}" for v in violations
    )
