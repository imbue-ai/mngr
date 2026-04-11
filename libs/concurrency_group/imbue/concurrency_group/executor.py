import threading
from concurrent.futures import Future
from contextlib import AbstractContextManager
from typing import Any
from typing import Callable
from typing import TypeVar

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

T = TypeVar("T")

# Global default callback invoked in each worker thread after its task completes.
# Libraries should set this early at startup (e.g. in their plugin_manager setup)
# to clean up thread-local resources that would otherwise leak file descriptors.
_default_on_thread_exit: Callable[[], None] | None = None


def set_default_on_thread_exit(callback: Callable[[], None] | None) -> None:
    """Set a global default thread-exit callback for all ConcurrencyGroupExecutor instances.

    This is intended to be called once at application startup. The callback
    is invoked in each worker thread after its submitted callable completes,
    and should clean up any thread-local resources (e.g. gevent Hubs) that
    hold OS-level file descriptors.
    """
    global _default_on_thread_exit
    _default_on_thread_exit = callback


class ConcurrencyGroupExecutor(AbstractContextManager):
    """Executor that runs callables in threads managed by a ConcurrencyGroup.

    After each submitted callable completes, the global default thread-exit
    callback (set via ``set_default_on_thread_exit``) is invoked to clean up
    thread-local resources.
    """

    def __init__(
        self,
        parent_cg: ConcurrencyGroup,
        name: str,
        max_workers: int,
    ) -> None:
        self._parent_cg = parent_cg
        self._name = name
        self._semaphore = threading.BoundedSemaphore(max_workers)
        self._cg: ConcurrencyGroup | None = None

    def __enter__(self) -> "ConcurrencyGroupExecutor":
        self._cg = self._parent_cg.make_concurrency_group(
            name=self._name,
            exit_timeout_seconds=float("inf"),
        )
        self._cg.__enter__()
        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        assert self._cg is not None
        self._cg.__exit__(exc_type, exc_val, exc_tb)

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> "Future[T]":
        """Submit a callable for concurrent execution."""
        assert self._cg is not None
        future: Future[T] = Future()

        def _run() -> None:
            with self._semaphore:
                try:
                    result = fn(*args, **kwargs)
                except Exception as e:
                    future.set_exception(e)
                else:
                    future.set_result(result)
                finally:
                    if _default_on_thread_exit is not None:
                        _default_on_thread_exit()

        self._cg.start_new_thread(
            target=_run,
            name=getattr(fn, "__name__", None),
            is_checked=False,
        )
        return future
