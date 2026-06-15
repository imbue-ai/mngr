from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import _coerce_exit_code


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


def test_call_runs_mngr_version_in_forkserver_child() -> None:
    """End-to-end: a real ``mngr --version`` runs in a forkserver child.

    This exercises the whole mechanism: starting the forkserver, preloading
    ``imbue.mngr.main``, forking a child, running the CLI, and capturing
    stdout/exit-code. ``--version`` is used because it does no provider
    discovery, so the call is fast and deterministic.
    """
    result = MngrCaller().call(["--version"], timeout=120.0)
    assert result.returncode == 0
    assert result.is_timed_out is False
    assert "mngr" in result.stdout


def test_call_reports_nonzero_exit_for_unknown_command() -> None:
    result = MngrCaller().call(["definitely-not-a-real-subcommand"], timeout=120.0)
    assert result.returncode != 0
