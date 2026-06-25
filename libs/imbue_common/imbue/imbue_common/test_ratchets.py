from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc

_DIR = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")


# The shared Sentry error-reporting machinery (``imbue_common/sentry/``) is ported from
# sentry_sdk's own integration patterns and the minds backend. It legitimately relies on
# patterns that imbue_common otherwise holds at a stricter bar than the apps that consume
# it (``cast``, ``getattr``, ``functools.partial``, nested helper functions, logging
# handler ``__init__``s, underscore-prefixed imports from sentry_sdk, and the ``asyncio``
# import used to filter out ``CancelledError``). Rather than loosen these ratchets for the
# whole library, we exclude only that subpackage from them (the same way it is excluded
# from the coverage denominator in pyproject.toml) and keep the original baselines for the
# rest of imbue_common.
_SENTRY_SUBPACKAGE_EXCLUSION: tuple[str, ...] = ("*/sentry/*",)


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(15))


def test_prevent_exec() -> None:
    rc.check_exec(_DIR, snapshot(2))


def test_prevent_eval() -> None:
    rc.check_eval(_DIR, snapshot(2))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    rc.check_time_sleep(_DIR, snapshot(1))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(7))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(1))


def test_prevent_broad_exception_catch() -> None:
    # The added catches are all in the shared Sentry error-reporting machinery
    # (``imbue_common/sentry/``): the before_send wrapper, the traceback formatter,
    # the custom HTTP transport, the loguru callback runner, and the S3 uploader all
    # deliberately catch ``Exception`` so a failure inside error reporting can never
    # crash the calling process or lose the original event. These were ported here
    # from the minds backend so ``mngr latchkey forward`` can share them.
    rc.check_broad_exception_catch(_DIR, snapshot(8))


def test_prevent_base_exception_catch() -> None:
    rc.check_base_exception_catch(_DIR, snapshot(3))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


def test_prevent_silent_decode_error_catches() -> None:
    rc.check_silent_decode_error_catches(_DIR, snapshot(1))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(1))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


def test_prevent_import_datetime() -> None:
    rc.check_import_datetime(_DIR, snapshot(0))


def test_prevent_importlib_import_module() -> None:
    rc.check_importlib_import_module(_DIR, snapshot(2))


def test_prevent_getattr() -> None:
    chunks = rc.check_ratchet_rule(rc.PREVENT_GETATTR, _DIR, rc._SELF_EXCLUSION + _SENTRY_SUBPACKAGE_EXCLUSION)
    assert len(chunks) <= snapshot(20), rc.PREVENT_GETATTR.format_failure(chunks)


def test_prevent_setattr() -> None:
    rc.check_setattr(_DIR, snapshot(9))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    chunks = rc.check_ratchet_rule(rc.PREVENT_ASYNCIO_IMPORT, _DIR, rc._SELF_EXCLUSION + _SENTRY_SUBPACKAGE_EXCLUSION)
    assert len(chunks) <= snapshot(0), rc.PREVENT_ASYNCIO_IMPORT.format_failure(chunks)


def test_prevent_pandas_import() -> None:
    rc.check_pandas_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))


def test_prevent_namedtuple() -> None:
    rc.check_namedtuple(_DIR, snapshot(0))


def test_prevent_yaml_usage() -> None:
    rc.check_yaml_usage(_DIR, snapshot(2))


def test_prevent_functools_partial() -> None:
    chunks = rc.check_ratchet_rule(
        rc.PREVENT_FUNCTOOLS_PARTIAL, _DIR, rc._SELF_EXCLUSION + _SENTRY_SUBPACKAGE_EXCLUSION
    )
    assert len(chunks) <= snapshot(2), rc.PREVENT_FUNCTOOLS_PARTIAL.format_failure(chunks)


def test_prevent_exit_stack() -> None:
    rc.check_exit_stack(_DIR, snapshot(0))


# --- Hardcoded paths ---


def test_prevent_hardcoded_claude_dir() -> None:
    rc.check_hardcoded_claude_dir(_DIR, snapshot(0))


def test_prevent_hardcoded_guarded_binary() -> None:
    rc.check_hardcoded_guarded_binary(_DIR, snapshot(0))


# --- Naming conventions ---


def test_prevent_num_prefix() -> None:
    rc.check_num_prefix(_DIR, snapshot(1))


# --- Documentation ---


def test_prevent_trailing_comments() -> None:
    rc.check_trailing_comments(_DIR, snapshot(8))


def test_prevent_init_docstrings() -> None:
    rc.check_init_docstrings(_DIR, snapshot(0))


@pytest.mark.timeout(10)
def test_prevent_args_in_docstrings() -> None:
    rc.check_args_in_docstrings(_DIR, snapshot(1))


@pytest.mark.timeout(10)
def test_prevent_returns_in_docstrings() -> None:
    rc.check_returns_in_docstrings(_DIR, snapshot(2))


# --- Type safety ---


def test_prevent_literal_with_multiple_options() -> None:
    rc.check_literal_with_multiple_options(_DIR, snapshot(0))


def test_prevent_bare_generic_types() -> None:
    rc.check_bare_generic_types(_DIR, snapshot(1))


def test_prevent_typing_builtin_imports() -> None:
    rc.check_typing_builtin_imports(_DIR, snapshot(0))


def test_prevent_short_uuid_ids() -> None:
    rc.check_short_uuid_ids(_DIR, snapshot(1))


# --- Pydantic / models ---


def test_prevent_model_copy() -> None:
    rc.check_model_copy(_DIR, snapshot(4))


# --- Logging ---


def test_prevent_fstring_logging() -> None:
    rc.check_fstring_logging(_DIR, snapshot(1))


def test_prevent_click_echo() -> None:
    rc.check_click_echo(_DIR, snapshot(2))


def test_prevent_logger_exception() -> None:
    rc.check_logger_exception(_DIR, snapshot(0))


# --- Testing conventions ---


def test_prevent_unittest_mock_imports() -> None:
    rc.check_unittest_mock_imports(_DIR, snapshot(1))


def test_prevent_monkeypatch_setattr() -> None:
    rc.check_monkeypatch_setattr(_DIR, snapshot(2))


def test_prevent_test_container_classes() -> None:
    rc.check_test_container_classes(_DIR, snapshot(0))


def test_prevent_pytest_mark_integration() -> None:
    rc.check_pytest_mark_integration(_DIR, snapshot(2))


# --- Process management ---


def test_prevent_os_fork() -> None:
    rc.check_os_fork(_DIR, snapshot(4))


def test_prevent_bare_urwid_tty_signal_keys() -> None:
    rc.check_bare_urwid_tty_signal_keys(_DIR, snapshot(0))


def test_prevent_direct_subprocess() -> None:
    rc.check_direct_subprocess(_DIR, snapshot(14))


def test_prevent_bare_tmux_targets() -> None:
    rc.check_bare_tmux_targets(_DIR, snapshot(0))


# --- AST-based ratchets ---


def test_prevent_if_elif_without_else() -> None:
    chunks = rc.find_if_elif_without_else(_DIR, rc._SELF_EXCLUSION + _SENTRY_SUBPACKAGE_EXCLUSION)
    assert len(chunks) <= snapshot(3), rc.PREVENT_IF_ELIF_WITHOUT_ELSE.format_failure(chunks)


def test_prevent_inline_functions() -> None:
    chunks = rc.find_inline_functions(_DIR, _SENTRY_SUBPACKAGE_EXCLUSION)
    assert len(chunks) <= snapshot(3), rc.PREVENT_INLINE_FUNCTIONS.format_failure(chunks)


def test_prevent_underscore_imports() -> None:
    chunks = rc.find_underscore_imports(_DIR, _SENTRY_SUBPACKAGE_EXCLUSION)
    assert len(chunks) <= snapshot(3), rc.PREVENT_UNDERSCORE_IMPORTS.format_failure(chunks)


def test_prevent_init_methods_in_non_exception_classes() -> None:
    chunks = rc.find_init_methods_in_non_exception_classes(_DIR, _SENTRY_SUBPACKAGE_EXCLUSION)
    assert len(chunks) <= snapshot(2), rc.PREVENT_INIT_IN_NON_EXCEPTION_CLASSES.format_failure(chunks)


def test_prevent_cast_usage() -> None:
    chunks = rc.find_cast_usages(_DIR, _SENTRY_SUBPACKAGE_EXCLUSION)
    assert len(chunks) <= snapshot(0), rc.PREVENT_CAST_USAGE.format_failure(chunks)


def test_prevent_assert_isinstance() -> None:
    rc.check_assert_isinstance(_DIR, snapshot(1))


def test_prevent_per_file_host_upload() -> None:
    rc.check_per_file_host_upload(_DIR, snapshot(0))


# --- Project-level checks ---


def test_prevent_code_in_init_files() -> None:
    rc.check_code_in_init_files(_DIR, snapshot(0))
