from collections.abc import Iterator

import pytest

from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import _coerce_exit_code


@pytest.fixture()
def mngr_caller() -> Iterator[MngrCaller]:
    """A standalone caller whose warm processes are torn down after the test.

    A real :meth:`MngrCaller.call` leaves an idle warm process waiting on a
    socket for the next call. ``stop`` terminates it (and tears down the owned
    concurrency group + socket directory) so the per-session leak checker does
    not flag the lingering subprocess.
    """
    caller = MngrCaller()
    try:
        yield caller
    finally:
        caller.stop()


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


# These tests spawn a real warm ``mngr`` process (a fresh interpreter that
# imports ``imbue.mngr.main``) and run the CLI in it over a socket. Under CI load
# that cold start routinely exceeds the 10s global pytest-timeout (the call's own
# timeout is 120s), so give them a generous per-test timeout and mark them flaky
# so offload retries a contended cold start rather than failing the run.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_runs_mngr_version_in_warm_process(mngr_caller: MngrCaller) -> None:
    """End-to-end: a real ``mngr --version`` runs in a warm process.

    This exercises the whole mechanism: spawning a warm process connected by an
    anonymous socketpair, handing it the argv over the socket, running the CLI,
    and capturing stdout/exit-code. ``--version`` is used because it does no
    provider discovery, so the call is fast and deterministic.

    Marked flaky: warm-process cold-start occasionally exceeds the 10s pytest
    timeout under CI load.
    """
    result = mngr_caller.call(["--version"], timeout=120.0)
    assert result.returncode == 0
    assert result.is_timed_out is False
    assert "mngr" in result.stdout


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_reports_nonzero_exit_for_unknown_command(mngr_caller: MngrCaller) -> None:
    # Marked flaky: warm-process cold-start occasionally exceeds the 10s pytest
    # timeout under CI load.
    result = mngr_caller.call(["definitely-not-a-real-subcommand"], timeout=120.0)
    assert result.returncode != 0


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_second_call_reuses_pre_spawned_warm_process(mngr_caller: MngrCaller) -> None:
    """After one call, a replacement warm process is already waiting for the next.

    The first call pays the cold-start cost; the second should be served by the
    warm process spawned when the first was claimed. We assert correctness of
    both results (timing is not asserted, to avoid flakiness).
    """
    first_result = mngr_caller.call(["--version"], timeout=120.0)
    assert first_result.returncode == 0
    second_result = mngr_caller.call(["--version"], timeout=120.0)
    assert second_result.returncode == 0
    assert "mngr" in second_result.stdout


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_call_times_out_and_reports_timed_out(mngr_caller: MngrCaller) -> None:
    """A zero timeout surfaces as a timed-out result with a sentinel returncode."""
    result = mngr_caller.call(["--version"], timeout=0.0)
    assert result.is_timed_out is True
    assert result.returncode != 0
