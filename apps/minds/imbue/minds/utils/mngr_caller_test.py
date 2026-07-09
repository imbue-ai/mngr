import os
from collections.abc import Iterator
from pathlib import Path

import click
import pytest

from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import _coerce_exit_code
from imbue.minds.utils.mngr_caller import _execute_mngr_cli
from imbue.mngr.utils.polling import wait_for


def _make_cwd_capturing_command(captured: list[str]) -> click.Command:
    """Build a tiny stand-in CLI that records the process working directory.

    The directory is captured into ``captured`` (rather than printed) so the test
    can assert on the cwd the CLI actually ran in.
    """

    @click.command()
    def _command() -> None:
        captured.append(os.getcwd())

    return _command


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


def test_execute_mngr_cli_changes_to_requested_cwd(tmp_path: Path) -> None:
    """A non-None ``cwd`` makes the CLI run from that directory.

    ``_execute_mngr_cli`` runs in the throwaway warm process, so ``os.chdir`` is
    safe there; the test restores its own cwd afterwards.
    """
    captured_cwd: list[str] = []
    original_cwd = Path.cwd()
    try:
        returncode, _stdout, _stderr = _execute_mngr_cli(_make_cwd_capturing_command(captured_cwd), (), {}, tmp_path)
    finally:
        os.chdir(original_cwd)
    assert returncode == 0
    assert Path(captured_cwd[0]).resolve() == tmp_path.resolve()


def test_execute_mngr_cli_keeps_cwd_when_none() -> None:
    """A ``None`` ``cwd`` leaves the working directory untouched."""
    captured_cwd: list[str] = []
    original_cwd = Path.cwd()
    returncode, _stdout, _stderr = _execute_mngr_cli(_make_cwd_capturing_command(captured_cwd), (), {}, None)
    assert returncode == 0
    assert Path(captured_cwd[0]).resolve() == original_cwd.resolve()
    assert Path.cwd() == original_cwd


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


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_warm_process_exits_when_parent_disconnects(mngr_caller: MngrCaller) -> None:
    """An idle warm process must not hang around once its parent socket is closed.

    Closing the parent end without sending a request simulates the minds backend
    going away (e.g. a hard kill). The warm process should observe EOF on its
    socket and exit on its own, leaving no orphan.
    """
    warm_process = mngr_caller._spawn_warm_process()
    warm_process.connection.close()
    wait_for(
        warm_process.running_process.is_finished,
        timeout=30.0,
        poll_interval=0.05,
        error_message="warm mngr process did not exit after its parent disconnected",
    )
    assert warm_process.running_process.is_finished()
