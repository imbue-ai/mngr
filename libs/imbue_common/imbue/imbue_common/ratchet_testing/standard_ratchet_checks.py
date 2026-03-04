from pathlib import Path

from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_ARGS_IN_DOCSTRINGS
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_ASSERT_ISINSTANCE
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_ASYNCIO_IMPORT
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
from imbue.imbue_common.ratchet_testing.common_ratchets import RegexRatchetRule
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.ratchets import _is_test_file
from imbue.imbue_common.ratchet_testing.ratchets import find_assert_isinstance_usages
from imbue.imbue_common.ratchet_testing.ratchets import find_cast_usages
from imbue.imbue_common.ratchet_testing.ratchets import find_code_in_init_files
from imbue.imbue_common.ratchet_testing.ratchets import find_if_elif_without_else
from imbue.imbue_common.ratchet_testing.ratchets import find_init_methods_in_non_exception_classes
from imbue.imbue_common.ratchet_testing.ratchets import find_inline_functions
from imbue.imbue_common.ratchet_testing.ratchets import find_underscore_imports

_SELF_EXCLUSION: tuple[str, ...] = ("test_ratchets.py", "standard_ratchet_checks.py")


def assert_ratchet(rule: RegexRatchetRule, source_dir: Path, max_count: int) -> None:
    """Check a regex-based ratchet rule and assert the count is within the limit."""
    chunks = check_ratchet_rule(rule, source_dir, _SELF_EXCLUSION)
    assert len(chunks) <= max_count, rule.format_failure(chunks)


# --- Code safety ---


def check_todos(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_TODOS, source_dir, max_count)


def check_exec(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_EXEC, source_dir, max_count)


def check_eval(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_EVAL, source_dir, max_count)


def check_while_true(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_WHILE_TRUE, source_dir, max_count)


def check_time_sleep(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_TIME_SLEEP, source_dir, max_count)


def check_global_keyword(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_GLOBAL_KEYWORD, source_dir, max_count)


def check_bare_print(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_BARE_PRINT, source_dir, max_count)


# --- Exception handling ---


def check_bare_except(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_BARE_EXCEPT, source_dir, max_count)


def check_broad_exception_catch(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_BROAD_EXCEPTION_CATCH, source_dir, max_count)


def check_base_exception_catch(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_BASE_EXCEPTION_CATCH, source_dir, max_count)


def check_builtin_exception_raises(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_BUILTIN_EXCEPTION_RAISES, source_dir, max_count)


# --- Import style ---


def check_inline_imports(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_INLINE_IMPORTS, source_dir, max_count)


def check_relative_imports(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_RELATIVE_IMPORTS, source_dir, max_count)


def check_import_datetime(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_IMPORT_DATETIME, source_dir, max_count)


def check_importlib_import_module(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_IMPORTLIB_IMPORT_MODULE, source_dir, max_count)


def check_getattr(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_GETATTR, source_dir, max_count)


def check_setattr(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_SETATTR, source_dir, max_count)


# --- Banned libraries and patterns ---


def check_asyncio_import(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_ASYNCIO_IMPORT, source_dir, max_count)


def check_pandas_import(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_PANDAS_IMPORT, source_dir, max_count)


def check_dataclasses_import(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_DATACLASSES_IMPORT, source_dir, max_count)


def check_namedtuple(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_NAMEDTUPLE, source_dir, max_count)


def check_yaml_usage(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_YAML_USAGE, source_dir, max_count)


def check_functools_partial(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_FUNCTOOLS_PARTIAL, source_dir, max_count)


# --- Naming conventions ---


def check_num_prefix(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_NUM_PREFIX, source_dir, max_count)


# --- Documentation ---


def check_trailing_comments(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_TRAILING_COMMENTS, source_dir, max_count)


def check_init_docstrings(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_INIT_DOCSTRINGS, source_dir, max_count)


def check_args_in_docstrings(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_ARGS_IN_DOCSTRINGS, source_dir, max_count)


def check_returns_in_docstrings(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_RETURNS_IN_DOCSTRINGS, source_dir, max_count)


# --- Type safety ---


def check_literal_with_multiple_options(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_LITERAL_MULTIPLE_OPTIONS, source_dir, max_count)


def check_bare_generic_types(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_BARE_GENERIC_TYPES, source_dir, max_count)


def check_typing_builtin_imports(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_TYPING_BUILTIN_IMPORTS, source_dir, max_count)


def check_short_uuid_ids(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_SHORT_UUID_IDS, source_dir, max_count)


# --- Pydantic / models ---


def check_model_copy(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_MODEL_COPY, source_dir, max_count)


# --- Logging ---


def check_fstring_logging(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_FSTRING_LOGGING, source_dir, max_count)


def check_click_echo(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_CLICK_ECHO, source_dir, max_count)


# --- Testing conventions ---


def check_unittest_mock_imports(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_UNITTEST_MOCK_IMPORTS, source_dir, max_count)


def check_monkeypatch_setattr(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_MONKEYPATCH_SETATTR, source_dir, max_count)


def check_test_container_classes(source_dir: Path, max_count: int) -> None:
    all_chunks = check_ratchet_rule(PREVENT_TEST_CONTAINER_CLASSES, source_dir, _SELF_EXCLUSION)
    chunks = tuple(c for c in all_chunks if _is_test_file(c.file_path))
    assert len(chunks) <= max_count, PREVENT_TEST_CONTAINER_CLASSES.format_failure(chunks)


def check_pytest_mark_integration(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_PYTEST_MARK_INTEGRATION, source_dir, max_count)


# --- Process management ---


def check_os_fork(source_dir: Path, max_count: int) -> None:
    assert_ratchet(PREVENT_OS_FORK, source_dir, max_count)


def check_direct_subprocess(
    source_dir: Path,
    max_count: int,
    excluded_patterns: tuple[str, ...] = TEST_FILE_PATTERNS,
) -> None:
    chunks = check_ratchet_rule(PREVENT_DIRECT_SUBPROCESS, source_dir, excluded_patterns)
    assert len(chunks) <= max_count, PREVENT_DIRECT_SUBPROCESS.format_failure(chunks)


# --- AST-based ratchets ---


def check_if_elif_without_else(source_dir: Path, max_count: int) -> None:
    chunks = find_if_elif_without_else(source_dir, _SELF_EXCLUSION)
    assert len(chunks) <= max_count, PREVENT_IF_ELIF_WITHOUT_ELSE.format_failure(chunks)


def check_inline_functions(source_dir: Path, max_count: int) -> None:
    chunks = find_inline_functions(source_dir)
    assert len(chunks) <= max_count, PREVENT_INLINE_FUNCTIONS.format_failure(chunks)


def check_underscore_imports(source_dir: Path, max_count: int) -> None:
    chunks = find_underscore_imports(source_dir)
    assert len(chunks) <= max_count, PREVENT_UNDERSCORE_IMPORTS.format_failure(chunks)


def check_init_methods_in_non_exception_classes(source_dir: Path, max_count: int) -> None:
    chunks = find_init_methods_in_non_exception_classes(source_dir)
    assert len(chunks) <= max_count, PREVENT_INIT_IN_NON_EXCEPTION_CLASSES.format_failure(chunks)


def check_cast_usage(source_dir: Path, max_count: int) -> None:
    chunks = find_cast_usages(source_dir)
    assert len(chunks) <= max_count, PREVENT_CAST_USAGE.format_failure(chunks)


def check_assert_isinstance(source_dir: Path, max_count: int) -> None:
    chunks = find_assert_isinstance_usages(source_dir)
    assert len(chunks) <= max_count, PREVENT_ASSERT_ISINSTANCE.format_failure(chunks)


# --- Project-level checks ---


def check_code_in_init_files(
    source_dir: Path,
    max_count: int,
    allowed_root_init_lines: set[str] | None = None,
) -> None:
    """Ensure __init__.py files are empty (per style guide)."""
    kwargs = {"allowed_root_init_lines": allowed_root_init_lines} if allowed_root_init_lines else {}
    violations = find_code_in_init_files(source_dir, **kwargs)
    assert len(violations) <= max_count, (
        "Code found in __init__.py files (should be empty per style guide):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
