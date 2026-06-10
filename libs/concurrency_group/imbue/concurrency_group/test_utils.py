import time
from collections.abc import Callable
from typing import Final

# A deliberately unusual sleep duration for long-lived placeholder subprocesses that tests start and
# then terminate/kill themselves. Using a globally-unique value (rather than a common "sleep 30")
# avoids any chance of collision with unrelated processes if a test ever identifies a process by its
# command line. The value is large enough that the process never exits on its own during a test.
LONG_SLEEP_SECONDS: Final[str] = "36284"


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
