from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc

_DIR = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(2))


def test_prevent_time_sleep() -> None:
    rc.check_time_sleep(_DIR, snapshot(1))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(0))


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
    rc.check_silent_decode_error_catches(_DIR, snapshot(0))


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
    # 4: server.py has always been asyncio (the proxy is an async ASGI app), and
    # cli.py now does `asyncio.run(hypercorn.asyncio.serve(...))` to run that app
    # in-process -- the necessary replacement for uvicorn's sync `.run()` (which
    # itself ran an asyncio loop). Hypercorn exposes no non-asyncio serve path
    # for an in-process app object. cli_test.py exercises the serve loop's TLS
    # teardown behavior (bounded SSL shutdown + exception handler), which can
    # only be tested from inside an asyncio loop. server_test.py is the fourth,
    # for the same reason: the stall notice is an event-loop timer, so its stub
    # backend must yield to the loop to let the timer run at all, and the
    # streaming stall/close regression tests drive the async streaming response
    # against a stub asyncio socket backend.
    rc.check_asyncio_import(_DIR, snapshot(4))


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
    # 48: the six additions over the historical 42 are all in cli_test.py's
    # hypercorn serving-path tests (a minimal lifespan-only ASGI app and a
    # shutdown trigger), which necessarily run inside the asyncio loop under
    # test.
    # 50: two more awaits in server.py's WebSocket forwarder, which is
    # inherently async (FastAPI WS handler): racing the two relay legs with
    # asyncio.wait and explicitly closing the client leg when the backend
    # dies, so a send-quiet client cannot be left half-open forever.
    # 60: the stall notice and the client-disconnect race. server.py waits on
    # the ASGI receive channel and races it against the backend request, and
    # server_test.py drives that path through the raw ASGI interface
    # (TestClient only delivers a disconnect after the response is complete,
    # which is the ordering under test) with a stub backend awaiting a sleep --
    # a blocking sleep would hold the event loop and stop both the timer and
    # the disconnect from ever being observed.
    # 84: server.py's stall-guarded streaming response bounds every client write
    # and deterministically closes the backend stream (the SSE pool saturation
    # fix), the same disconnect race now also guards the SSE backend handoff,
    # and server_test.py's regression tests for both drive the real async
    # response, ASGI channel stubs, and a stub SSE backend -- all of which is
    # necessarily async/await code.
    # 94: closing the backend stream when a client disconnect ties with the SSE
    # handoff, plus the regression test for it -- which has to construct the tie
    # deterministically (an asyncio.Barrier releasing the stub backend and the
    # ASGI receive channel in the same event loop step), so it is async by
    # construction.
    # 97: the streaming response, rather than its body generator, now awaits the
    # backend close, so it also runs on the one exit path a generator-owned
    # close cannot cover (a stall on the response headers, before the generator
    # is ever started); the regression test for that path drives the real async
    # response against the async stub send channel.
    # 104: the end-to-end test that pins the production wiring -- it drives the
    # real handler against a real socket backend over a real one-connection pool
    # and then re-uses that pool, none of which can be observed outside the
    # event loop the streaming response runs in.
    # 105: the tie test's stub backend yields the loop once before rendezvousing
    # with the client's disconnect, which is what puts the disconnect at the
    # rendezvous first and so leaves the handoff free to finish before the
    # ``asyncio.wait`` waiter resumes -- the ordering the tie branch is about.
    # 109: the buffered path reads the response headers and the body in two
    # steps instead of one ``request()``, which is what tells a backend that
    # never answered from one that answered and then dropped the body. Splitting
    # them means awaiting the send, the read, and the close separately, and they
    # stay in one coroutine so the caller still races a single task against the
    # client disconnect.
    # 114: witnessing that event streams reach the client incrementally. The
    # claim is about arrival *ordering*, so the test has to interleave with the
    # streaming producer; TestClient's synchronous API only returns once the
    # response is complete, which is exactly the information the unit is about.
    # Observing it means driving the real handler and its async send channel
    # against an async stub backend.
    rc.check_async_await(_DIR, snapshot(114))


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
    # Most of these are the inline ``noqa: SLF001`` suppressions on
    # stream_manager_test.py's private-access test hooks; ruff requires the
    # suppression on the flagged line, so each hook costs one.
    rc.check_trailing_comments(_DIR, snapshot(30))


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
    rc.check_literal_with_multiple_options(_DIR, snapshot(1))


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


def test_prevent_bare_urwid_tty_signal_keys() -> None:
    rc.check_bare_urwid_tty_signal_keys(_DIR, snapshot(0))


def test_prevent_direct_subprocess() -> None:
    rc.check_direct_subprocess(_DIR, snapshot(1))


def test_prevent_bare_tmux_targets() -> None:
    rc.check_bare_tmux_targets(_DIR, snapshot(0))


# --- AST-based ratchets ---


def test_prevent_if_elif_without_else() -> None:
    rc.check_if_elif_without_else(_DIR, snapshot(0))


def test_prevent_inline_functions() -> None:
    rc.check_inline_functions(_DIR, snapshot(8))


def test_prevent_underscore_imports() -> None:
    rc.check_underscore_imports(_DIR, snapshot(0))


def test_prevent_init_methods_in_non_exception_classes() -> None:
    # 2: InMemoryTLSConfig subclasses hypercorn's third-party `Config` (a plain,
    # non-model class) and needs `__init__` to call super().__init__() and hold
    # the per-instance in-memory SSLContext. It cannot be a pydantic model, and
    # setting the context from outside the class would evade this ratchet while
    # doing the same thing, so the __init__ stays.
    # 3: _StallGuardedStreamingResponse subclasses starlette's StreamingResponse
    # (a plain, non-model third-party class) and needs `__init__` to call
    # super().__init__() and hold the typed body generator + send timeout.
    rc.check_init_methods_in_non_exception_classes(_DIR, snapshot(3))


def test_prevent_cast_usage() -> None:
    rc.check_cast_usage(_DIR, snapshot(0))


def test_prevent_assert_isinstance() -> None:
    rc.check_assert_isinstance(_DIR, snapshot(0))


def test_prevent_per_file_host_upload() -> None:
    rc.check_per_file_host_upload(_DIR, snapshot(0))


# --- Project-level checks ---


def test_prevent_code_in_init_files() -> None:
    rc.check_code_in_init_files(_DIR, snapshot(1))


# --- Modal images ---


def test_prevent_unpinned_modal_pip_install() -> None:
    rc.check_unpinned_modal_pip_install(_DIR, snapshot(0))
