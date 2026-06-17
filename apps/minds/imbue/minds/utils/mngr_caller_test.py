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


@pytest.mark.timeout(30)
def test_call_runs_mngr_version_in_forkserver_child() -> None:
    """End-to-end: a real ``mngr --version`` runs in a forkserver child.

    This exercises the whole mechanism: starting the forkserver, preloading
    ``imbue.mngr.main``, forking a child, running the CLI, and capturing
    stdout/exit-code. ``--version`` is used because it does no provider
    discovery, so the call is fast and deterministic.

    The slow part is ``child.start()`` -- a cold forkserver boot + preload of all
    of ``imbue.mngr.main`` -- which the ``_stop_forkserver_after_test`` fixture
    forces on every test (it tears the forkserver down after each). That preload
    is bounded by the *per-test* timeout (not the ``call`` timeout, which only
    bounds the in-child CLI run), so it is raised from the default 10s -- which it
    occasionally exceeds on a loaded CI runner -- to 30s, with flaky retries for
    the tail. The ``call`` timeout caps just the (fast) CLI run.
    """
    result = MngrCaller().call(["--version"], timeout=20.0)
    assert result.returncode == 0
    assert result.is_timed_out is False
    assert "mngr" in result.stdout


@pytest.mark.timeout(30)
def test_call_reports_nonzero_exit_for_unknown_command() -> None:
    # Same cold-start exposure as the version test above (forkserver boot +
    # ``imbue.mngr.main`` preload in ``child.start()``), so the per-test timeout
    # is raised to 30s and flaky retries are enabled; the ``call`` timeout caps
    # just the fast in-child CLI run.
    result = MngrCaller().call(["definitely-not-a-real-subcommand"], timeout=20.0)
    assert result.returncode != 0
