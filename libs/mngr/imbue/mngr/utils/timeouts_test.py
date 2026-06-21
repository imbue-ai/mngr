import threading

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.timeouts import call_with_timeout


def test_call_with_timeout_returns_result_of_fast_function() -> None:
    with ConcurrencyGroup(name="test-timeout-fast") as cg:
        result = call_with_timeout(lambda: 36284, timeout_seconds=5.0, concurrency_group=cg, description="fast work")
    assert result == 36284


def test_call_with_timeout_propagates_function_exception() -> None:
    class _Boom(RuntimeError):
        pass

    def _raise() -> int:
        raise _Boom("kaboom-36284")

    with ConcurrencyGroup(name="test-timeout-raise") as cg:
        with pytest.raises(_Boom, match="kaboom-36284"):
            call_with_timeout(_raise, timeout_seconds=5.0, concurrency_group=cg, description="raising work")


def test_call_with_timeout_raises_timeout_when_function_is_too_slow() -> None:
    # The worker blocks until we release it; the call must give up after the timeout.
    release_event = threading.Event()

    def _block_until_released() -> int:
        release_event.wait(5.0)
        return 1

    try:
        with ConcurrencyGroup(name="test-timeout-slow") as cg:
            with pytest.raises(TimeoutError, match="did not complete within"):
                call_with_timeout(
                    _block_until_released,
                    timeout_seconds=0.2,
                    concurrency_group=cg,
                    description="slow work",
                )
    finally:
        # Let the abandoned daemon worker finish so it does not linger.
        release_event.set()
