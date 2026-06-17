import multiprocessing.forkserver
import multiprocessing.resource_tracker
from collections.abc import Iterator

import pytest

from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import _coerce_exit_code


@pytest.fixture(autouse=True)
def _stop_forkserver_after_test() -> Iterator[None]:
    """Tear down the multiprocessing forkserver + resource_tracker after each test.

    A real :meth:`MngrCaller.call` starts a process-lifetime forkserver (and
    multiprocessing starts a resource_tracker alongside it). In production these
    are reaped at interpreter exit / on parent death, but the per-session leak
    checker runs earlier and would flag them, so tests that start a real
    forkserver must stop it explicitly. Both ``_stop`` calls are safe no-ops
    when nothing was started.
    """
    yield
    # ``_stop`` is the real reset mechanism (multiprocessing itself calls it at
    # exit); it resets internal state so a later call restarts cleanly. It is
    # absent from typeshed, hence the ignores.
    multiprocessing.forkserver._forkserver._stop()  # ty: ignore[unresolved-attribute]
    multiprocessing.resource_tracker._resource_tracker._stop()  # ty: ignore[unresolved-attribute]


def test_coerce_exit_code_none_is_success() -> None:
    assert _coerce_exit_code(None) == 0


def test_coerce_exit_code_passes_through_ints() -> None:
    assert _coerce_exit_code(0) == 0
    assert _coerce_exit_code(2) == 2


def test_coerce_exit_code_string_message_is_failure() -> None:
    # click/SystemExit with a string code conventionally means an error.
    assert _coerce_exit_code("boom") == 1


def test_call_result_defaults() -> None:
    result = MngrCallResult(returncode=0)
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.is_timed_out is False


@pytest.mark.flaky
def test_call_runs_mngr_version_in_forkserver_child() -> None:
    """End-to-end: a real ``mngr --version`` runs in a forkserver child.

    This exercises the whole mechanism: starting the forkserver, preloading
    ``imbue.mngr.main``, forking a child, running the CLI, and capturing
    stdout/exit-code. ``--version`` is used because it does no provider
    discovery, so the call is fast and deterministic.

    Marked flaky: forkserver cold-start occasionally exceeds the 10s pytest
    timeout under CI load.
    """
    result = MngrCaller().call(["--version"], timeout=120.0)
    assert result.returncode == 0
    assert result.is_timed_out is False
    assert "mngr" in result.stdout


@pytest.mark.flaky
def test_call_reports_nonzero_exit_for_unknown_command() -> None:
    # Marked flaky: forkserver cold-start occasionally exceeds the 10s pytest
    # timeout under CI load.
    result = MngrCaller().call(["definitely-not-a-real-subcommand"], timeout=120.0)
    assert result.returncode != 0
