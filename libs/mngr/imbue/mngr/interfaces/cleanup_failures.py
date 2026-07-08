from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CleanupFailure


class CleanupFailedError(MngrError):
    """A single real cleanup failure, as a raisable exception.

    This is the leaf type of a :class:`CleanupFailedGroup`. It carries the structured
    :class:`CleanupFailure` so the aggregation boundary can recover the category, message,
    and agent/host context for exit-code selection and JSON output. See
    ``specs/cleanup-error-aggregation.md``.
    """

    def __init__(self, failure: CleanupFailure) -> None:
        super().__init__(f"[{failure.category}] {failure.message}")
        self.failure = failure


class CleanupFailedGroup(ExceptionGroup[CleanupFailedError]):
    """Raised by a cleanup operation that left one or more real resources behind.

    Each leaf is a :class:`CleanupFailedError` wrapping a structured :class:`CleanupFailure`.
    Cleanup operations (``Host.destroy_agent``, ``Host.stop_agents``, ``ProviderInstance.destroy_host``)
    are aggregate-and-continue: they attempt every step, collect every real failure, and raise
    this group *once* at the end. Returning normally means cleanup fully succeeded or only
    benign "already gone" outcomes occurred.

    The interface raises rather than returns so a real failure can never be silently dropped by
    a caller that forgets to inspect a return value (see ``specs/cleanup-error-aggregation.md``).
    """

    @classmethod
    def from_failures(cls, failures: Sequence[CleanupFailure]) -> "CleanupFailedGroup":
        assert failures, "CleanupFailedGroup requires at least one failure"
        return cls("cleanup left one or more resources behind", [CleanupFailedError(f) for f in failures])

    @property
    def failures(self) -> tuple[CleanupFailure, ...]:
        """The structured failures carried by this group's leaves, in order.

        ``from_failures`` only ever builds a flat group of ``CleanupFailedError`` leaves, but
        ``ExceptionGroup.exceptions`` is typed as possibly holding nested groups, so we recurse
        into any nested group too -- that way no failure can be dropped regardless of shape.
        """
        return tuple(_iter_cleanup_failures(self))


def _iter_cleanup_failures(group: BaseExceptionGroup[CleanupFailedError]) -> Iterator[CleanupFailure]:
    """Yield every leaf's structured ``CleanupFailure``, recursing through nested groups.

    ``group.exceptions`` is ``CleanupFailedError | BaseExceptionGroup[CleanupFailedError]``; the
    two cases are exhaustive, so the match needs no catch-all.
    """
    for leaf in group.exceptions:
        match leaf:
            case CleanupFailedError():
                yield leaf.failure
            case BaseExceptionGroup():
                yield from _iter_cleanup_failures(leaf)


@contextmanager
def collecting_cleanup_failures() -> Iterator[list[CleanupFailure]]:
    """Aggregate the real failures of a best-effort cleanup operation, raising at the end.

    Yields a list that the operation appends :class:`CleanupFailure` records to (directly, or via
    :func:`collect_cleanup_failures` for sub-operations that themselves raise). On exit, if any
    failures were collected, raises a :class:`CleanupFailedGroup`; if none were, returns normally.

    This makes "leftover resources" impossible to drop silently: the operation never returns a
    list a caller can ignore, and a forgotten sub-step that raises propagates rather than vanishing.
    """
    failures: list[CleanupFailure] = []
    yield failures
    if failures:
        raise CleanupFailedGroup.from_failures(failures)


def collect_cleanup_failures(sink: list[CleanupFailure], group: CleanupFailedGroup) -> None:
    """Absorb the failures of a sub-cleanup that raised into the enclosing aggregate.

    Use at a call site that invokes another raising cleanup operation (e.g. ``Host.destroy_agent``
    calling ``Host.stop_agents``) so the sub-operation's failures are aggregated rather than
    aborting the enclosing cleanup:

        with collecting_cleanup_failures() as failures:
            try:
                self.stop_agents([agent.id])
            except CleanupFailedGroup as group:
                collect_cleanup_failures(failures, group)
            # ... remaining steps still run and append to `failures`
    """
    sink.extend(group.failures)
