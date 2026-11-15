"""Give a test that drives a real child program the wall-clock budget its class costs.

The repo's global ``--timeout=10`` is sized for in-process Python. A test that execs a
child program pays process startup instead -- a fresh interpreter, a bash script, git, a
nested pytest -- which costs one to two orders of magnitude more wall time and scales with
machine load rather than with the work the test does. Measured across 25,676 tests, the
3,326 that exec a child program during their call phase cost a median of 0.20s on the
offload CI runners, p99 5.1s and p999 8.9s; the other 22,350 cost a median of 0.067s and
p99 0.71s. Against a 10s deadline the second group has two orders of magnitude of headroom
and the first has almost none, which is why a loaded runner turns the first group red (MIND-212 -> MIND-243 -> MIND-247 -> MIND-278) and leaves the second alone.

So the budget is a property of the class, the way ``_MARKER_TIMEOUT_SECONDS`` in
``conftest_hooks`` makes it one for tmux and release tests. What this module adds is that
membership is *observed* rather than declared: 348 of the repo's 1,119 test files hold a
member, so no marker anyone has to remember could cover the class, and every attempt to
cover it by decorating today's members has been undone by tomorrow's new test. A test joins
the class by execing a child program, which is the same event that makes it expensive.

The deadline only ever moves later, only once per test, and never for a test that declares a
``@pytest.mark.timeout`` of its own -- declaring a budget is how a test says it differs from
its class, and that keeps winning.
"""

import signal
import sys
import threading
from types import FrameType
from typing import Final

import pytest
import pytest_timeout
from pydantic import ConfigDict

from imbue.imbue_common.mutable_model import MutableModel

# Sized from the class's cost on the CI runners themselves, read off the offload run history
# rather than a developer machine: the slowest member's median is 5.2s and the slowest
# observation of any member is 9.6s (truncated there by the 10s deadline this replaces).
# 60s absorbs ~12x the slowest member, matching both the headroom and the number the tmux
# class was given for comparable work. Revise it by re-measuring the class on CI.
SUBPROCESS_CLASS_TIMEOUT_SECONDS: Final[int] = 60

# The CPython audit events that mean "this process is starting a child program". These
# strings are event names, not calls: this module starts nothing itself.
#
# The plain fork event is deliberately absent: it fires in the child as well, and a child
# that forks without execing is not running a program whose startup anyone is paying for.
_CHILD_PROGRAM_AUDIT_EVENTS: Final[frozenset[str]] = frozenset(
    {"subprocess.Popen", "os.posix_spawn", "os.spawn", "os.exec", "os.system"}
)


class _ArmedDeadline(MutableModel):
    """A deadline of ours that is currently armed, and the test it will fail.

    ``extended_timeout_seconds`` is the budget that replaced the test's original one, so a
    failure can report the budget that was actually enforced; it stays None until (and
    unless) the test execs something.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    item: pytest.Item
    settings: pytest_timeout.Settings
    extended_timeout_seconds: float | None = None


class _BudgetSlot:
    """The one deadline this process can have armed, or None between tests.

    A CPython audit hook is called with a fixed signature and cannot be handed state, and the
    SIGALRM timer it has to reach is per-process anyway, so this slot is held once at module
    level and written in place rather than rebound.
    """

    armed: _ArmedDeadline | None = None


_budget_slot: Final[_BudgetSlot] = _BudgetSlot()


def _resolve_extended_timeout(
    seconds_armed_with: float,
    seconds_remaining: float,
    has_explicit_timeout: bool,
    budget_seconds: int = SUBPROCESS_CLASS_TIMEOUT_SECONDS,
) -> float | None:
    """The seconds to re-arm the live deadline with, or None to leave it alone.

    The class budget is measured from the moment the deadline was armed -- the elapsed time
    is what the armed timer has already spent -- so a test that execs a child program gets
    exactly the budget a collection-time marker would have given it, however far into the
    test the first spawn happens. It is declined when the deadline is already at least that
    generous -- a test with a bigger budget of its own, or a run passing
    ``pytest --timeout=300`` while debugging -- and when nothing is left on the clock, so a
    spawn from a thread that outlived its test cannot resurrect a dead deadline.
    """
    if has_explicit_timeout:
        return None
    if seconds_remaining <= 0.0:
        return None
    extended_remaining = budget_seconds - (seconds_armed_with - seconds_remaining)
    if extended_remaining <= seconds_remaining:
        return None
    return extended_remaining


def _resolve_enforced_settings(
    settings: pytest_timeout.Settings, extended_timeout_seconds: float | None
) -> pytest_timeout.Settings:
    """The settings the failure should report: the budget that was actually enforced.

    pytest-timeout renders its message from the settings it is handed, so a deadline we moved
    has to be reported at the budget it moved to.
    """
    if extended_timeout_seconds is None:
        return settings
    return settings._replace(timeout=extended_timeout_seconds)


def _extend_deadline_for_child_program() -> None:
    """Move the running test's deadline out to the class budget, at its first spawn."""
    armed = _budget_slot.armed
    if armed is None or armed.extended_timeout_seconds is not None:
        return
    extended_remaining = _resolve_extended_timeout(
        seconds_armed_with=armed.settings.timeout,
        seconds_remaining=signal.getitimer(signal.ITIMER_REAL)[0],
        has_explicit_timeout=armed.item.get_closest_marker("timeout") is not None,
    )
    if extended_remaining is None:
        return
    armed.extended_timeout_seconds = SUBPROCESS_CLASS_TIMEOUT_SECONDS
    # setitimer, unlike signal(), is a plain syscall with no main-thread restriction, which is
    # what lets this reach the deadline from the worker thread ConcurrencyGroup spawns on.
    signal.setitimer(signal.ITIMER_REAL, extended_remaining)


def _observe_audit_event(event: str, args: tuple[object, ...]) -> None:
    """Audit hook body: cheap enough to sit on every audited event in the process."""
    if event in _CHILD_PROGRAM_AUDIT_EVENTS:
        _extend_deadline_for_child_program()


def _fail_at_deadline(signum: int, frame: FrameType | None) -> None:
    """SIGALRM handler: fail the test the way pytest-timeout's own signal path does.

    It stays installed once armed, rather than being restored to the default the way
    pytest-timeout restores it. A deadline can be moved from any thread (see
    ``_extend_deadline_for_child_program``), so an alarm can outlive its test by the width of
    that race; with the default handler in place that alarm would kill the whole pytest
    worker, and with this one it finds no armed deadline and does nothing.
    """
    __tracebackhide__ = True
    armed = _budget_slot.armed
    if armed is None:
        return
    pytest_timeout.timeout_sigalrm(
        armed.item, _resolve_enforced_settings(armed.settings, armed.extended_timeout_seconds)
    )


def _cancel_deadline() -> None:
    """Disarm: the cancel callback pytest-timeout's own cancel hook reaches for on the item."""
    signal.setitimer(signal.ITIMER_REAL, 0)
    _budget_slot.armed = None


# Installed at import: an audit hook cannot be removed, and a module body runs once per
# process, so importing the timer below cannot leave its observer uninstalled.
sys.addaudithook(_observe_audit_event)


@pytest.hookimpl(tryfirst=True)
def pytest_timeout_set_timer(item: pytest.Item, settings: pytest_timeout.Settings) -> bool | None:
    """Arm pytest-timeout's SIGALRM deadline ourselves, so a spawn can move it later.

    This is pytest-timeout's documented hook for an alternative timer, and arms exactly what
    its own signal path arms, including the cancel callback that path leaves on the item.
    Returning None hands back a run it cannot serve -- the thread timer, or a session whose
    tests run off the main thread -- leaving those tests on the budget they have today.
    """
    if settings.method != "signal" or threading.current_thread() is not threading.main_thread():
        return None
    item.cancel_timeout = _cancel_deadline  # ty: ignore[unresolved-attribute]
    _budget_slot.armed = _ArmedDeadline(item=item, settings=settings)
    signal.signal(signal.SIGALRM, _fail_at_deadline)
    signal.setitimer(signal.ITIMER_REAL, settings.timeout)
    return True
