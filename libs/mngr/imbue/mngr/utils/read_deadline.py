import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

# The wall-clock deadline (time.monotonic() seconds) bounding host reads on the current
# execution context, or None when reads are unbounded. A ContextVar (not thread-local) so it
# is correctly isolated per pool thread and per gevent greenlet: the code that sets it and the
# host reads it bounds run in the same context, while other contexts are unaffected.
_read_deadline_monotonic: ContextVar[float | None] = ContextVar("mngr_host_read_deadline_monotonic", default=None)

# Floor applied to a clamped read timeout. Kept at a whole second because the shell-command path
# converts the timeout to an int, where anything below 1 truncates to 0 and is treated as "no
# timeout". A read that trips a passed deadline therefore self-terminates within ~1s.
_MIN_READ_TIMEOUT_SECONDS: Final[float] = 1.0


@contextmanager
def reads_bounded_for(timeout_seconds: float) -> Iterator[None]:
    """Bound every host read issued on this execution context to a shared wall-clock budget.

    While active, each command or file read a host issues is given the remaining budget as its
    timeout, so a slow or wedged host self-terminates its reads (surfacing as HostConnectionError)
    within the budget instead of hanging. Nesting replaces the budget for the inner scope.
    """
    token = _read_deadline_monotonic.set(time.monotonic() + timeout_seconds)
    try:
        yield
    finally:
        _read_deadline_monotonic.reset(token)


def remaining_read_timeout(explicit_timeout_seconds: float | None) -> float | None:
    """Clamp a read's timeout to the active read budget (see ``reads_bounded_for``), if any.

    Returns the explicit timeout unchanged when no budget is active. Otherwise returns the smaller
    of the explicit timeout and the remaining budget, floored at ``_MIN_READ_TIMEOUT_SECONDS``.
    """
    deadline = _read_deadline_monotonic.get()
    if deadline is None:
        return explicit_timeout_seconds
    remaining = max(_MIN_READ_TIMEOUT_SECONDS, deadline - time.monotonic())
    if explicit_timeout_seconds is None:
        return remaining
    return min(explicit_timeout_seconds, remaining)
