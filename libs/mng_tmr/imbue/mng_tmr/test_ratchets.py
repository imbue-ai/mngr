from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.ratchets import check_no_ruff_errors
from imbue.imbue_common.ratchet_testing.ratchets import check_no_type_errors

_DIR = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec_usage() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval_usage() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    # Ratchet at 6: launch_all_test_agents (1 launch-delay)
    # + launch_and_poll_agents (2 poll sleeps) + wait_for_integrator (2 poll sleeps)
    # + read_agent_result (1 retry delay) -- all human-sanctioned
    rc.check_time_sleep(_DIR, snapshot(7))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(0))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    # Ratchet at 1: _try_list_agents wraps list_agents for transient provider errors (human-sanctioned)
    rc.check_broad_exception_catch(_DIR, snapshot(1))


def test_prevent_base_exception_catch() -> None:
    rc.check_base_exception_catch(_DIR, snapshot(0))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(0))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


def test_prevent_import_datetime() -> None:
    rc.check_import_datetime(_DIR, snapshot(0))


def test_prevent_importlib_import_module() -> None:
    rc.check_importlib_import_module(_DIR, snapshot(0))


def test_prevent_getattr() -> None:
    rc.check_getattr(_DIR, snapshot(0))


def test_prevent_setattr() -> None:
    rc.check_setattr(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    rc.check_asyncio_import(_DIR, snapshot(0))


def test_prevent_pandas_import() -> None:
    rc.check_pandas_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))


def test_prevent_namedtuple_usage() -> None:
    rc.check_namedtuple(_DIR, snapshot(0))


def test_prevent_yaml_usage() -> None:
    rc.check_yaml_usage(_DIR, snapshot(0))


def test_prevent_functools_partial() -> None:
    rc.check_functools_partial(_DIR, snapshot(0))


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
    rc.check_short_uuid_ids(_DIR, snapshot(0))


# --- Pydantic / models ---


def test_prevent_model_copy() -> None:
    rc.check_model_copy(_DIR, snapshot(0))


# --- Logging ---


def test_prevent_fstring_logging() -> None:
    rc.check_fstring_logging(_DIR, snapshot(0))


def test_prevent_click_echo() -> None:
    rc.check_click_echo(_DIR, snapshot(0))


# --- Testing conventions ---


def test_prevent_unittest_mock_imports() -> None:
    rc.check_unittest_mock_imports(_DIR, snapshot(0))


def test_prevent_monkeypatch_setattr() -> None:
    rc.check_monkeypatch_setattr(_DIR, snapshot(0))


def test_prevent_test_container_classes() -> None:
    rc.check_test_container_classes(_DIR, snapshot(0))


def test_prevent_pytest_mark_integration() -> None:
    rc.check_pytest_mark_integration(_DIR, snapshot(0))


# --- Process management ---


def test_prevent_os_fork() -> None:
    rc.check_os_fork(_DIR, snapshot(0))


def test_prevent_direct_subprocess_usage() -> None:
    rc.check_direct_subprocess(_DIR, snapshot(0))


# --- AST-based ratchets ---


def test_prevent_if_elif_without_else() -> None:
    rc.check_if_elif_without_else(_DIR, snapshot(0))


def test_prevent_inline_functions_in_non_test_code() -> None:
    rc.check_inline_functions(_DIR, snapshot(0))


def test_prevent_importing_underscore_prefixed_names_in_non_test_code() -> None:
    rc.check_underscore_imports(_DIR, snapshot(0))


def test_prevent_init_methods_in_non_exception_classes() -> None:
    rc.check_init_methods_in_non_exception_classes(_DIR, snapshot(0))


def test_prevent_cast_usage() -> None:
    rc.check_cast_usage(_DIR, snapshot(0))


def test_prevent_assert_isinstance_usage() -> None:
    rc.check_assert_isinstance(_DIR, snapshot(0))


# --- Project-level checks ---


def test_prevent_code_in_init_files() -> None:
    rc.check_code_in_init_files(_DIR, snapshot(0))


def test_no_type_errors() -> None:
    """Ensure the codebase has zero type errors."""
    check_no_type_errors(_DIR)


def test_no_ruff_errors() -> None:
    """Ensure the codebase has zero ruff linting errors."""
    check_no_ruff_errors(_DIR)
