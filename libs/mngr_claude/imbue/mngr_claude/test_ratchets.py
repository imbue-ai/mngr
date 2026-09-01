from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_INIT_IN_NON_EXCEPTION_CLASSES
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_SILENT_DECODE_ERROR_CATCH
from imbue.imbue_common.ratchet_testing.common_ratchets import PREVENT_TIME_SLEEP
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.ratchets import find_init_methods_in_non_exception_classes
from imbue.imbue_common.ratchet_testing.ratchets import find_silent_decode_error_catches

_DIR = Path(__file__).parent.parent.parent

# Standalone resource scripts run outside the mngr process and can only use
# Python stdlib, so ratchets that require mngr abstractions do not apply.
_STANDALONE_RESOURCE_SCRIPTS: tuple[str, ...] = (
    "sync_keychain_credentials.py",
    "stream_snapshot.py",
)
# common_transcript_convert.py is standalone too, but it is waived only for bare print
# (it prints its appended-event count to stdout for common_transcript.sh to capture, its
# data interface) and __init__ methods (stdlib only, so no pydantic models).
_COMMON_TRANSCRIPT_CONVERT_SCRIPT: tuple[str, ...] = ("common_transcript_convert.py",)

pytestmark = pytest.mark.xdist_group(name="ratchets")


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    # Standalone resource scripts are long-running poll-loop daemons that must
    # sleep between polls and cannot use mngr's wait helpers.
    chunks = check_ratchet_rule(PREVENT_TIME_SLEEP, _DIR, _STANDALONE_RESOURCE_SCRIPTS)
    assert len(chunks) <= snapshot(0), PREVENT_TIME_SLEEP.format_failure(chunks)


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(
        _DIR, snapshot(0), excluded_patterns=_STANDALONE_RESOURCE_SCRIPTS + _COMMON_TRANSCRIPT_CONVERT_SCRIPT
    )


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    rc.check_broad_exception_catch(_DIR, snapshot(0))


def test_prevent_base_exception_catch() -> None:
    rc.check_base_exception_catch(_DIR, snapshot(0))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


def test_prevent_silent_decode_error_catches() -> None:
    # No file is excluded, so every catch in the package is counted here. Three of the
    # allowed ones read a live-appended JSONL stream from a stdlib-only resource script
    # (no logger is importable, and anything written to stderr is reported as an error in
    # the agent's pane): two in common_transcript_convert.py (the raw transcript and the
    # converter's own output) and one in stream_snapshot.py (the raw transcript again).
    # A truncated trailing line caught mid-write is expected and benign there -- it
    # re-reads complete on the next poll -- so it is skipped silently rather than logged.
    # The other three are read probes over data mngr does not own: two credential
    # checks in plugin.py, where an unparsable .credentials.json or keychain blob just
    # means "not on a Claude subscription", and one in stream_json_impl.py, where a
    # non-JSON line is the documented normal case (blank lines and the debug output
    # claude leaks to stdout).
    chunks = find_silent_decode_error_catches(_DIR, TEST_FILE_PATTERNS)
    assert len(chunks) <= snapshot(6), PREVENT_SILENT_DECODE_ERROR_CATCH.format_failure(chunks)


# --- Import style ---


def test_prevent_inline_imports() -> None:
    # The single deferred import lives in stream_json.py's _impl() accessor: it lazily loads
    # stream_json_impl (which pulls the ~900-module anthropic SDK) only when a Claude stream is
    # produced/consumed, keeping anthropic off the mngr cold-start path (MIND-179). This is the
    # same intentional pattern as mngr's help_formatter lazy rich import.
    rc.check_inline_imports(_DIR, snapshot(1))


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


def test_prevent_namedtuple() -> None:
    rc.check_namedtuple(_DIR, snapshot(0))


def test_prevent_yaml_usage() -> None:
    rc.check_yaml_usage(_DIR, snapshot(0))


def test_prevent_functools_partial() -> None:
    rc.check_functools_partial(_DIR, snapshot(0))


def test_prevent_exit_stack() -> None:
    rc.check_exit_stack(_DIR, snapshot(0))


def test_prevent_async_await() -> None:
    rc.check_async_await(_DIR, snapshot(0))


# --- Hardcoded paths ---


def test_prevent_hardcoded_claude_dir() -> None:
    rc.check_hardcoded_claude_dir(_DIR, snapshot(0))


def test_prevent_hardcoded_guarded_binary() -> None:
    rc.check_hardcoded_guarded_binary(_DIR, snapshot(0))


# --- Naming conventions ---


def test_prevent_num_prefix() -> None:
    rc.check_num_prefix(_DIR, snapshot(0))


# --- Documentation ---


def test_prevent_trailing_comments() -> None:
    rc.check_trailing_comments(_DIR, snapshot(1))


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


def test_prevent_logger_exception() -> None:
    rc.check_logger_exception(_DIR, snapshot(0))


# --- Testing conventions ---


def test_prevent_unittest_mock_imports() -> None:
    rc.check_unittest_mock_imports(_DIR, snapshot(1))


def test_prevent_monkeypatch_setattr() -> None:
    rc.check_monkeypatch_setattr(_DIR, snapshot(1))


def test_prevent_test_container_classes() -> None:
    rc.check_test_container_classes(_DIR, snapshot(0))


def test_prevent_pytest_mark_integration() -> None:
    rc.check_pytest_mark_integration(_DIR, snapshot(0))


# --- Process management ---


def test_prevent_os_fork() -> None:
    rc.check_os_fork(_DIR, snapshot(0))


def test_prevent_bare_urwid_tty_signal_keys() -> None:
    rc.check_bare_urwid_tty_signal_keys(_DIR, snapshot(0))


def test_prevent_direct_subprocess() -> None:
    rc.check_direct_subprocess(_DIR, snapshot(0), TEST_FILE_PATTERNS + _STANDALONE_RESOURCE_SCRIPTS)


def test_prevent_bare_tmux_targets() -> None:
    rc.check_bare_tmux_targets(_DIR, snapshot(0))


# --- AST-based ratchets ---


def test_prevent_if_elif_without_else() -> None:
    rc.check_if_elif_without_else(_DIR, snapshot(0))


def test_prevent_inline_functions() -> None:
    rc.check_inline_functions(_DIR, snapshot(0))


def test_prevent_underscore_imports() -> None:
    rc.check_underscore_imports(_DIR, snapshot(0))


def test_prevent_init_methods_in_non_exception_classes() -> None:
    # Standalone resource scripts (stdlib only) cannot use pydantic models, so
    # their small state classes legitimately define __init__.
    chunks = find_init_methods_in_non_exception_classes(
        _DIR, _STANDALONE_RESOURCE_SCRIPTS + _COMMON_TRANSCRIPT_CONVERT_SCRIPT
    )
    assert len(chunks) <= snapshot(0), PREVENT_INIT_IN_NON_EXCEPTION_CLASSES.format_failure(chunks)


def test_prevent_cast_usage() -> None:
    rc.check_cast_usage(_DIR, snapshot(0))


def test_prevent_assert_isinstance() -> None:
    rc.check_assert_isinstance(_DIR, snapshot(0))


def test_prevent_per_file_host_upload() -> None:
    rc.check_per_file_host_upload(_DIR, snapshot(1))


# --- Project-level checks ---


def test_prevent_code_in_init_files() -> None:
    rc.check_code_in_init_files(_DIR, snapshot(1))


# --- Modal images ---


def test_prevent_unpinned_modal_pip_install() -> None:
    rc.check_unpinned_modal_pip_install(_DIR, snapshot(0))
