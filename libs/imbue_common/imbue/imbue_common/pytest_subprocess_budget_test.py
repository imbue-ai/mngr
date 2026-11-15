"""Tests for the observed subprocess-class test budget."""

import pytest
import pytest_timeout

from imbue.imbue_common.pytest_subprocess_budget import SUBPROCESS_CLASS_TIMEOUT_SECONDS
from imbue.imbue_common.pytest_subprocess_budget import _ArmedDeadline
from imbue.imbue_common.pytest_subprocess_budget import _resolve_enforced_settings
from imbue.imbue_common.pytest_subprocess_budget import _resolve_extended_timeout

pytest_plugins = ["pytester"]


# What every pytester run below installs: the timer, and nothing else of the repo's shared
# hooks (whose session work includes taking the global test lock the outer run is holding).
_PYTESTER_CONFTEST = """\
from imbue.imbue_common.pytest_subprocess_budget import pytest_timeout_set_timer
"""

# The deadline the inner runs start from, small enough to hit inside a test. It stands in for
# the repo's --timeout=10, and like it is sized for work that stays in the process.
_INNER_DEFAULT_TIMEOUT_SECONDS = 1

# How long an inner test's child program runs. Three of them together outlast the inner
# default deadline, the way a real member of this class outlasts the repo's.
_CHILD_PROGRAM_SECONDS = 0.6

# Long enough that an inner test blocking for it hits whichever deadline is being pinned.
_PAST_ANY_DEADLINE_SECONDS = 30


def _settings(timeout: float) -> pytest_timeout.Settings:
    return pytest_timeout.Settings(timeout=timeout, method="signal", func_only=True, disable_debugger_detection=False)


def test_a_test_on_the_default_budget_is_offered_the_class_budget() -> None:
    assert _resolve_extended_timeout(
        seconds_armed_with=10.0, seconds_remaining=10.0, has_explicit_timeout=False, budget_seconds=60
    ) == pytest.approx(60.0)


def test_the_class_budget_is_measured_from_when_the_deadline_was_armed() -> None:
    """A spawn 9s into a 10s deadline leaves 51s, not a fresh 60s: the budget covers the test."""
    assert _resolve_extended_timeout(
        seconds_armed_with=10.0, seconds_remaining=1.0, has_explicit_timeout=False, budget_seconds=60
    ) == pytest.approx(51.0)


def test_an_explicitly_declared_timeout_beats_the_class_budget() -> None:
    """A test's own @pytest.mark.timeout is how it says it differs from its class."""
    assert (
        _resolve_extended_timeout(
            seconds_armed_with=30.0, seconds_remaining=30.0, has_explicit_timeout=True, budget_seconds=60
        )
        is None
    )


def test_a_more_generous_deadline_is_not_clamped_back_to_the_class_budget() -> None:
    """A generous `--timeout` passed while debugging must not be narrowed to the class budget."""
    assert (
        _resolve_extended_timeout(
            seconds_armed_with=300.0, seconds_remaining=300.0, has_explicit_timeout=False, budget_seconds=60
        )
        is None
    )


def test_a_deadline_already_at_the_class_budget_is_left_alone() -> None:
    assert (
        _resolve_extended_timeout(
            seconds_armed_with=60.0, seconds_remaining=60.0, has_explicit_timeout=False, budget_seconds=60
        )
        is None
    )


def test_no_deadline_is_armed_when_the_clock_has_run_out() -> None:
    """A spawn from a thread that outlived its test must not resurrect a dead deadline."""
    assert (
        _resolve_extended_timeout(
            seconds_armed_with=10.0, seconds_remaining=0.0, has_explicit_timeout=False, budget_seconds=60
        )
        is None
    )


def test_an_unextended_deadline_reports_the_budget_it_was_armed_with() -> None:
    assert _resolve_enforced_settings(_settings(10.0), extended_timeout_seconds=None).timeout == 10.0


def test_an_extended_deadline_reports_the_class_budget_rather_than_the_one_it_replaced() -> None:
    """A failure saying ">10.0s" after 60s is what sent this cluster through three misdiagnoses."""
    assert _resolve_enforced_settings(_settings(10.0), extended_timeout_seconds=60).timeout == 60


def test_an_extended_deadline_renders_the_message_pytest_timeout_will_print(request: pytest.FixtureRequest) -> None:
    """The whole round trip the SIGALRM handler makes, which only fires after a real 60s wait."""
    armed = _ArmedDeadline(item=request.node, settings=_settings(10.0))
    armed.extended_timeout_seconds = SUBPROCESS_CLASS_TIMEOUT_SECONDS

    enforced = _resolve_enforced_settings(armed.settings, armed.extended_timeout_seconds)

    assert pytest_timeout.PYTEST_FAILURE_MESSAGE % enforced.timeout == "Timeout (>60s) from pytest-timeout."


def test_the_class_budget_is_the_one_the_tmux_class_was_given() -> None:
    """One number for one shape of work: a test driving a real program outside this process."""
    assert SUBPROCESS_CLASS_TIMEOUT_SECONDS == 60


def _run_inner_pytest(pytester: pytest.Pytester, source: str) -> pytest.RunResult:
    pytester.makeconftest(_PYTESTER_CONFTEST)
    pytester.makepyfile(source)
    return pytester.runpytest_subprocess(
        f"--timeout={_INNER_DEFAULT_TIMEOUT_SECONDS}",
        "--timeout-method=signal",
        "-o",
        "timeout_func_only=true",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        "--no-header",
    )


def test_a_test_that_execs_a_child_program_outlives_the_in_process_default(pytester: pytest.Pytester) -> None:
    """The flake this exists for: honest child-process work past the in-process deadline."""
    result = _run_inner_pytest(
        pytester,
        f"""
import subprocess


def test_waits_on_children_past_the_default() -> None:
    for _ in range(3):
        subprocess.run(["sleep", "{_CHILD_PROGRAM_SECONDS}"], check=True)
""",
    )
    result.assert_outcomes(passed=1)


def test_the_deadline_a_spawning_test_ends_up_with_is_the_class_budget(pytester: pytest.Pytester) -> None:
    """Read off the live SIGALRM timer, so the budget is pinned end to end, not just resolved."""
    result = _run_inner_pytest(
        pytester,
        f"""
import signal
import subprocess


def test_reads_its_own_deadline() -> None:
    before = signal.getitimer(signal.ITIMER_REAL)[0]
    assert before <= {_INNER_DEFAULT_TIMEOUT_SECONDS}, before

    subprocess.run(["true"], check=True)

    after = signal.getitimer(signal.ITIMER_REAL)[0]
    assert {SUBPROCESS_CLASS_TIMEOUT_SECONDS} - 5 < after <= {SUBPROCESS_CLASS_TIMEOUT_SECONDS}, after
""",
    )
    result.assert_outcomes(passed=1)


def test_a_test_that_execs_nothing_still_dies_at_the_in_process_default(pytester: pytest.Pytester) -> None:
    """The budget follows the work: a test that execs nothing keeps the tight deadline."""
    result = _run_inner_pytest(
        pytester,
        f"""
import threading


def test_blocks_in_process() -> None:
    threading.Event().wait({_PAST_ANY_DEADLINE_SECONDS})
""",
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines([f"*Timeout (>{_INNER_DEFAULT_TIMEOUT_SECONDS}.0s) from pytest-timeout*"])


def test_a_test_whose_child_program_is_spawned_off_the_main_thread_is_covered(
    pytester: pytest.Pytester,
) -> None:
    """ConcurrencyGroup spawns on a worker thread, which is most of this repo's spawning."""
    result = _run_inner_pytest(
        pytester,
        f"""
import subprocess
import threading


def test_spawns_from_a_worker_thread() -> None:
    thread = threading.Thread(target=lambda: subprocess.run(["sleep", "{_CHILD_PROGRAM_SECONDS}"], check=True))
    thread.start()
    thread.join()
    threading.Event().wait(2)
""",
    )
    result.assert_outcomes(passed=1)


def test_a_test_that_declares_its_own_budget_is_not_given_the_class_budget(pytester: pytest.Pytester) -> None:
    """End to end on the rule the unit tests above state: the declared budget is the one enforced."""
    result = _run_inner_pytest(
        pytester,
        f"""
import subprocess
import threading

import pytest


@pytest.mark.timeout(2)
def test_declares_two_seconds_and_spawns() -> None:
    subprocess.run(["true"], check=True)
    threading.Event().wait({_PAST_ANY_DEADLINE_SECONDS})
""",
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Timeout (>2.0s) from pytest-timeout*"])
