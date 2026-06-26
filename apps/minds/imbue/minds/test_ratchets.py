from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS

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
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    # Six matches: ``destroying_test.py`` (a real test poll loop),
    # ``cli/env.py::_exec_into_recover`` (the 5-second auto-rollback
    # countdown -- a deliberate user-facing pause so the operator can
    # Ctrl-C if they want to intervene before recover fires),
    # ``deployment_tests/_mailtm.py::MailtmInbox._wait_for_message_body``
    # (polling the mail.tm HTTP API for an inbound email -- no
    # event-driven alternative without standing up an IMAP listener),
    # ``deployment_tests/helpers.py::_wait_for_url_alive`` (polling
    # the connector / litellm-proxy healthcheck URLs with cold-boot
    # tolerance, mirroring what ``envs/health_check.py`` does
    # deploy-side), ``deployment_tests/test_deploy_new_version.py::
    # _poll_for_deploy_id_change`` (polling /version after a redeploy
    # since Modal can keep routing to the stale container for a short
    # window after the swap), and ``deployment_tests/test_deploy_rollback.py::
    # _poll_for_deploy_id`` (polling /version after a forced auto-
    # rollback to confirm the rolled-back version is the one actually
    # serving traffic; same Modal swap-window justification).
    rc.check_time_sleep(_DIR, snapshot(9))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(12))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    # Most catches beyond the prior baseline are in the ported Sentry module
    # (``utils/sentry/``): the before_send wrapper, the traceback formatter, the
    # custom HTTP transport, the loguru callback runner, and the S3 uploader all
    # deliberately catch ``Exception`` so a failure inside error reporting can
    # never crash the app or lose the original event.
    #
    # The remaining one is in ``PermissionRequestsConsumer._run``: the consumer
    # thread is the request inbox's source of truth, so a single unprocessable
    # request must be logged (with traceback) and skipped rather than allowed to
    # kill the thread, which would silently stop every future permission request
    # from reaching the UI. The gateway validates requests up front, so this is a
    # defense-in-depth backstop, not the primary guard.
    rc.check_broad_exception_catch(_DIR, snapshot(17))


def test_prevent_base_exception_catch() -> None:
    rc.check_base_exception_catch(_DIR, snapshot(0))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


def test_prevent_silent_decode_error_catches() -> None:
    # The added catch is ``build_info.py`` parsing the desktop app's package.json
    # for the Sentry release id: a malformed file degrades to a fallback version
    # (logged at debug) rather than crashing startup.
    rc.check_silent_decode_error_catches(_DIR, snapshot(8))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    # The one allowed inline import is ``from imbue.mngr.main import cli`` inside
    # ``utils/mngr_caller.py``'s warm-server entry point. Importing it at module
    # scope would pay mngr's multi-second import cost inside the minds backend
    # process, defeating the entire purpose of the warm process (which imports it
    # out-of-process, off the request path). See that module's docstring.
    rc.check_inline_imports(_DIR, snapshot(1))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


def test_prevent_import_datetime() -> None:
    rc.check_import_datetime(_DIR, snapshot(0))


def test_prevent_importlib_import_module() -> None:
    rc.check_importlib_import_module(_DIR, snapshot(0))


def test_prevent_getattr() -> None:
    # Both usages are one line in the ported Sentry HTTP transport, reading the
    # response body whose attribute (``data`` vs ``content``) varies across
    # sentry-sdk / urllib3 versions.
    rc.check_getattr(_DIR, snapshot(2))


def test_prevent_setattr() -> None:
    rc.check_setattr(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    # The minds backend is synchronous (Flask) and uses no asyncio. The only remaining import is in
    # ``scripts/launch_to_msg_e2e.py``, a standalone Playwright e2e driver that runs its own event
    # loop in a separate process.
    rc.check_asyncio_import(_DIR, snapshot(1))


def test_prevent_pandas_import() -> None:
    rc.check_pandas_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))


def test_prevent_namedtuple() -> None:
    rc.check_namedtuple(_DIR, snapshot(0))


def test_prevent_yaml_usage() -> None:
    # 8 of these are filename references to `pnpm-workspace.yaml` /
    # `pnpm-lock.yaml` in scripts/build_test.py docstrings + assertion
    # messages -- pnpm mandates YAML for its config so we cannot pick
    # TOML there. The ratchet's `r"yaml"` regex catches the substring
    # in filenames as if it were `import yaml`; tightening the regex
    # belongs in libs/imbue_common which this branch is scoped out of.
    rc.check_yaml_usage(_DIR, snapshot(8))


def test_prevent_functools_partial() -> None:
    # All in the ported Sentry module: the import plus binding the before_send
    # wrapper and the per-file S3 upload callbacks. Rewriting these as nested
    # defs/lambdas would only trade the violation for an inline-function one.
    rc.check_functools_partial(_DIR, snapshot(3))


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
    # ``forward_cli.py`` carries one ``noqa: S603`` suppression next to
    # the ``subprocess.Popen`` call that spawns ``mngr forward``. The
    # S603 suppression must be on the same line as the call for ruff to
    # recognize it; the noqa marker is intentionally not in the
    # trailing-comment exempt list.
    rc.check_trailing_comments(_DIR, snapshot(3))


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
    # ``latchkey/_spawn.py`` intentionally uses ``subprocess.Popen`` with
    # ``start_new_session=True`` so that the spawned ``latchkey gateway``
    # outlives the minds desktop client. That is the opposite of what the
    # ratchet is designed to enforce (managed cleanup via ConcurrencyGroup),
    # so we exclude that tiny helper specifically; see its module docstring
    # for the full justification.
    #
    # ``forward_cli.py`` similarly uses ``subprocess.Popen`` directly so it
    # can hold a reference to the ``mngr forward`` plugin's ``Popen.pid``
    # for the ``SIGHUP``-bounce path. ``ConcurrencyGroup.RunningProcess``
    # does not expose the PID today; once it does (a separate cleanup spec
    # in the concurrency_group lib), this exclusion can be dropped.
    excluded = TEST_FILE_PATTERNS + (
        "testing.py",
        "scripts/*.py",
        "*/latchkey/_spawn.py",
        "*/desktop_client/forward_cli.py",
        # ``destroying.py`` spawns a detached ``bash -c '<mngr destroy ...>'``
        # so the destroy survives a minds-backend exit; same justification as
        # ``latchkey/_spawn.py``. See specs/detached-destroy-flow/spec.md.
        "*/desktop_client/destroying.py",
        # ``deployment_tests/helpers.py`` is functionally test-helper code
        # (only ever called from `*/deployment_tests/test_*.py`); it shells
        # out to `modal environment list` for a one-shot read-only probe.
        # Same exception as the ``testing.py`` pattern but lives under a
        # different filename for the deployment_tests subpackage.
        "*/deployment_tests/helpers.py",
        # ``desktop_client/e2e_workspace_runner.py`` is the shared driver
        # for the minds Electron e2e test and the Modal snapshot script
        # (``scripts/snapshot_minds_e2e_state.py``). It necessarily shells
        # out to ``electron``, ``git``, and ``uv run mngr destroy`` --
        # operator-tool subprocesses that have no ConcurrencyGroup-managed
        # equivalent (Electron is a long-lived UI host, git is one-shot,
        # ``mngr destroy`` is the clean-up call). Same justification class
        # as ``testing.py``: it is only ever called from test / operator
        # entrypoints, never from product code.
        "*/desktop_client/e2e_workspace_runner.py",
    )
    # The one allowed match is ``cli/env.py::_exec_into_recover``,
    # which uses ``os.execvp`` to REPLACE the current process with
    # ``minds env recover`` on deploy failure. That is the opposite of
    # "spawn a managed child" -- there's no subprocess to clean up,
    # and the whole point is for stdout/stderr/exit-code to flow
    # through to the operator's shell as if recover were the original
    # command. ConcurrencyGroup doesn't apply.
    rc.check_direct_subprocess(_DIR, snapshot(1), excluded_patterns=excluded)


def test_prevent_bare_tmux_targets() -> None:
    rc.check_bare_tmux_targets(_DIR, snapshot(0))


# --- AST-based ratchets ---


def test_prevent_if_elif_without_else() -> None:
    # Both violations are in apps/minds/scripts/launch_to_msg_e2e.py:
    # pre_run_sweep's cleanup dispatch (is_dir vs exists) and
    # _advance_approval's stage-machine switch. Both exhaustively cover
    # the values they branch on; an else: pass would be cosmetic. The two added
    # branches are in the ported Sentry transport/uploader and likewise
    # exhaustively handle their cases.
    rc.check_if_elif_without_else(_DIR, snapshot(4))


def test_prevent_inline_functions() -> None:
    # The added inline function is the ``record_loss`` helper nested in the
    # ported Sentry HTTP transport's ``_send_request`` (it closes over the
    # envelope being sent).
    rc.check_inline_functions(_DIR, snapshot(12))


def test_prevent_underscore_imports() -> None:
    # ``loguru_handler.py`` imports sentry-sdk's ``_IGNORED_LOGGERS`` registry,
    # the documented way to interoperate with sentry's logger-ignore mechanism.
    rc.check_underscore_imports(_DIR, snapshot(1))


def test_prevent_init_methods_in_non_exception_classes() -> None:
    # Both are the ported Sentry loguru ``logging.Handler`` subclasses, which
    # need ``__init__`` to set up their executor / flags around super().__init__.
    rc.check_init_methods_in_non_exception_classes(_DIR, snapshot(2))


def test_prevent_cast_usage() -> None:
    # All in the ported Sentry module: sentry-sdk's ``Event`` TypedDict types
    # ``extra`` as ``object`` and scope contexts are loosely typed, so reading
    # them back requires casts to satisfy the type checker.
    rc.check_cast_usage(_DIR, snapshot(6))


def test_prevent_assert_isinstance() -> None:
    rc.check_assert_isinstance(_DIR, snapshot(0))


def test_prevent_per_file_host_upload() -> None:
    rc.check_per_file_host_upload(_DIR, snapshot(0))


# --- Project-level checks ---


def test_prevent_code_in_init_files() -> None:
    rc.check_code_in_init_files(_DIR, snapshot(0))
