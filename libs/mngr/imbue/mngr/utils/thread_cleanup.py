"""Thread-local resource cleanup for ConcurrencyGroupExecutor threads.

Pyinfra uses gevent greenlets for reading subprocess output. Each thread that
touches gevent gets its own Hub with an OS-level pipe for event-loop wakeups.
Without explicit cleanup, that pipe leaks when the thread exits.

This module provides ``cleanup_thread_local_resources`` for use as the
``on_thread_exit`` callback on ``ConcurrencyGroupExecutor`` instances,
and ``mngr_executor`` as a convenience factory that wires it up.
"""

# No public API exists for checking Hub existence without creating one.
# gevent._hub_local is private but quasi-stable: gevent's own internals
# (thread.py, threadpool.py, _abstract_linkable.py, etc.) all import from it.
from gevent._hub_local import get_hub_if_exists

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor


def cleanup_thread_local_resources() -> None:
    """Release thread-local resources that would otherwise leak FDs.

    Called automatically at the end of each ConcurrencyGroupExecutor worker
    thread's lifetime to prevent file-descriptor leaks from gevent Hubs.
    """
    hub = get_hub_if_exists()
    if hub is None:
        return
    hub.destroy(destroy_loop=True)


def mngr_executor(
    parent_cg: ConcurrencyGroup,
    name: str,
    max_workers: int,
) -> ConcurrencyGroupExecutor:
    """Create a ConcurrencyGroupExecutor with gevent Hub cleanup wired in.

    Use this instead of constructing ConcurrencyGroupExecutor directly in mngr
    code that may run pyinfra operations in worker threads.
    """
    return ConcurrencyGroupExecutor(
        parent_cg=parent_cg,
        name=name,
        max_workers=max_workers,
        on_thread_exit=cleanup_thread_local_resources,
    )
