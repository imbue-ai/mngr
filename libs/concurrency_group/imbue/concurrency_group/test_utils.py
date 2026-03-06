import time
from collections.abc import Callable
from threading import Event


def wait_interval(timeout: float) -> None:
    """Wait for a specified interval using Event.wait instead of time.sleep."""
    Event().wait(timeout=timeout)


def poll_until(
    condition: Callable[[], bool],
    timeout: float = 5.0,
    poll_interval: float = 0.01,
) -> bool:
    """Poll until a condition becomes true or timeout expires.

    Returns True if the condition was met, False if timeout occurred.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if condition():
            return True
        time.sleep(poll_interval)
    return condition()
