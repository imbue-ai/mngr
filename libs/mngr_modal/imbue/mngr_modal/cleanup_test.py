from datetime import timedelta

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.utils.testing import ModalCleanupOutcome
from imbue.mngr_modal.cleanup import ModalTestEnvironmentSweepResult
from imbue.mngr_modal.cleanup import _failure_detail
from imbue.mngr_modal.cleanup import cleanup_old_modal_test_environments

_ENV_A = "mngr_test-2026-07-30-18-23-24-5b1f9a04e7c34d17"
_ENV_B = "mngr_test-2026-07-30-19-25-15-c82d6e40fa1b4937"
_ENV_C = "mngr_test-2026-08-17-22-47-45-9e3a7c15bd684f20"


def _finished(stdout: str = "", stderr: str = "", is_timed_out: bool = False) -> FinishedProcess:
    return FinishedProcess(
        returncode=1,
        stdout=stdout,
        stderr=stderr,
        command=("uv", "run", "modal", "environment", "list"),
        is_timed_out=is_timed_out,
        is_output_already_logged=False,
    )


def test_cleanup_separates_environments_that_are_gone_from_those_that_are_not() -> None:
    outcomes = {
        _ENV_A: ModalCleanupOutcome.DELETED,
        _ENV_B: ModalCleanupOutcome.FAILED,
        _ENV_C: ModalCleanupOutcome.NOT_FOUND,
    }

    result = cleanup_old_modal_test_environments(
        find_old_environments=lambda max_age: list(outcomes),
        sweep_environment=lambda environment_name: outcomes[environment_name],
    )

    # NOT_FOUND counts as gone: someone else deleted it, which is the outcome we wanted.
    assert result.deleted_environment_names == (_ENV_A, _ENV_C)
    assert result.failed_environment_names == (_ENV_B,)


def test_cleanup_sweeps_every_environment_even_after_one_fails() -> None:
    swept: list[str] = []
    outcomes = {
        _ENV_A: ModalCleanupOutcome.FAILED,
        _ENV_B: ModalCleanupOutcome.FAILED,
        _ENV_C: ModalCleanupOutcome.DELETED,
    }

    def find_old_environments(max_age: timedelta) -> list[str]:
        del max_age
        return list(outcomes)

    def sweep_environment(environment_name: str) -> ModalCleanupOutcome:
        swept.append(environment_name)
        return outcomes[environment_name]

    result = cleanup_old_modal_test_environments(
        find_old_environments=find_old_environments,
        sweep_environment=sweep_environment,
    )

    assert swept == [_ENV_A, _ENV_B, _ENV_C]
    assert result.failed_environment_names == (_ENV_A, _ENV_B)


def test_cleanup_of_an_empty_account_reports_nothing_failed() -> None:
    result = cleanup_old_modal_test_environments(
        find_old_environments=lambda max_age: [],
        sweep_environment=lambda name: ModalCleanupOutcome.DELETED,
    )

    assert result == ModalTestEnvironmentSweepResult(deleted_environment_names=(), failed_environment_names=())


def test_failure_detail_says_so_when_the_command_timed_out() -> None:
    # A timed-out modal invocation leaves both streams empty, so reporting stderr
    # alone would explain nothing.
    assert _failure_detail(_finished(is_timed_out=True)) == "timed out after 30.0s"


def test_failure_detail_prefers_stderr() -> None:
    assert _failure_detail(_finished(stdout="ignored", stderr="  Environment not found\n")) == "Environment not found"


def test_failure_detail_falls_back_to_stdout() -> None:
    assert _failure_detail(_finished(stdout="  something went wrong\n")) == "something went wrong"
