"""Thread-local resource cleanup for mngr's worker threads.

Pyinfra uses gevent greenlets for reading subprocess output. Each thread that
touches gevent gets its own Hub with an OS-level pipe for event-loop wakeups.
Without explicit cleanup, that pipe leaks when the thread exits.

``mngr_executor`` is a context manager that yields an executor-like object
whose ``submit`` wraps each submitted callable with a ``finally`` that
destroys the thread-local gevent Hub.
"""

import functools
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any
from typing import Callable
from typing import TypeVar

# No public API exists for checking Hub existence without creating one.
# gevent._hub_local is private but quasi-stable: gevent's own internals
# (thread.py, threadpool.py, _abstract_linkable.py, etc.) all import from it.
from gevent._hub_local import get_hub_if_exists
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel

T = TypeVar("T")


def cleanup_thread_local_resources() -> None:
    """Release thread-local resources that would otherwise leak FDs.

    Destroys the thread-local gevent Hub (and its OS-level pipe) if one exists.
    Safe to call on threads that never touched gevent -- it's a no-op there.
    """
    hub = get_hub_if_exists()
    if hub is None:
        return
    hub.destroy(destroy_loop=True)


class _MngrExecutor(FrozenModel):
    """Thin wrapper around ConcurrencyGroupExecutor that runs gevent Hub
    cleanup after each submitted callable completes.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    executor: ConcurrencyGroupExecutor = Field(description="The underlying executor")

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> "Future[T]":
        @functools.wraps(fn)
        def wrapped() -> T:
            try:
                return fn(*args, **kwargs)
            finally:
                cleanup_thread_local_resources()

        return self.executor.submit(wrapped)


@contextmanager
def mngr_executor(
    parent_cg: ConcurrencyGroup,
    name: str,
    max_workers: int,
) -> Iterator[_MngrExecutor]:
    """Context manager yielding an executor that cleans up gevent Hubs.

    Use this instead of ConcurrencyGroupExecutor in mngr code that may run
    pyinfra operations in worker threads, so thread-local gevent Hubs are
    destroyed when each task finishes.
    """
    with ConcurrencyGroupExecutor(parent_cg=parent_cg, name=name, max_workers=max_workers) as executor:
        yield _MngrExecutor(executor=executor)
