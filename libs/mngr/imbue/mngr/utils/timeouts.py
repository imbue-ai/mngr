import threading
from collections.abc import Callable
from typing import Any
from typing import TypeVar

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

_T = TypeVar("_T")


def _run_and_store(
    func: Callable[[], Any],
    result_by_key: dict[str, Any],
    done_event: threading.Event,
) -> None:
    """Run ``func`` and stash its result or exception, signalling completion via ``done_event``."""
    try:
        result_by_key["value"] = func()
    except Exception as exception:
        result_by_key["error"] = exception
    finally:
        done_event.set()


def call_with_timeout(
    func: Callable[[], _T],
    timeout_seconds: float,
    concurrency_group: ConcurrencyGroup,
    description: str,
) -> _T:
    """Run ``func`` on a background daemon thread and return its result, raising TimeoutError if it does not finish in time.

    The worker is started ``daemon=True`` and ``is_checked=False`` so that, on
    timeout, it is abandoned rather than joined: Python threads cannot be
    force-killed, and a daemon thread does not keep the process alive, so the
    abandoned worker simply ends when its blocking call returns (or when the
    process exits). Use only for read-only / idempotent work where abandoning a
    slow call is safe -- e.g. resolving cloud credentials, where the concern is a
    metadata-server (IMDS) probe hanging on a non-cloud host.
    """
    done_event = threading.Event()
    result_by_key: dict[str, Any] = {}

    concurrency_group.start_new_thread(
        target=_run_and_store,
        args=(func, result_by_key, done_event),
        name=f"timeout:{description}",
        daemon=True,
        is_checked=False,
    )
    if not done_event.wait(timeout_seconds):
        raise TimeoutError(f"{description} did not complete within {timeout_seconds:g} seconds")
    if "error" in result_by_key:
        raise result_by_key["error"]
    if "value" not in result_by_key:
        # The worker signalled completion without a value or an error, which can only happen
        # if it was interrupted by a BaseException (e.g. SystemExit). Treat it as not completed.
        raise TimeoutError(f"{description} did not complete")
    return result_by_key["value"]
