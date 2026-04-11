"""Thread-local resource cleanup for ConcurrencyGroupExecutor threads.

Pyinfra uses gevent greenlets for reading subprocess output. Each thread that
touches gevent gets its own Hub with an OS-level pipe for event-loop wakeups.
Without explicit cleanup, that pipe leaks when the thread exits.

This module provides a cleanup function registered as the global default
``on_thread_exit`` callback via ``set_default_on_thread_exit``.
"""

import gevent
import gevent.exceptions


def cleanup_thread_local_resources() -> None:
    """Release thread-local resources that would otherwise leak FDs.

    Called automatically at the end of each ConcurrencyGroupExecutor worker
    thread's lifetime to prevent file-descriptor leaks from gevent Hubs.
    """
    try:
        hub = gevent.get_hub()
    except gevent.exceptions.LoopExit:
        return
    hub.destroy(destroy_loop=True)
