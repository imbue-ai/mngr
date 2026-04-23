import threading
from concurrent.futures import Future
from contextlib import AbstractContextManager
from typing import Any
from typing import Callable
from typing import TypeVar

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

T = TypeVar("T")


class ConcurrencyGroupExecutor(AbstractContextManager):
    """Executor that runs callables in threads managed by a ConcurrencyGroup."""

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

        self._cg.start_new_thread(
            target=_run,
            name=getattr(fn, "__name__", None),
            is_checked=False,
        )
        return future
