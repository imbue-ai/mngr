import resource
import time
from collections.abc import Callable
from typing import Final

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel

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


class IdleChildrenCpuMeasurement(FrozenModel):
    """How much CPU a process spent waiting for a batch of idle background children to exit."""

    cpu_seconds: float = Field(description="User plus system CPU the measuring process used while waiting")
    wall_seconds: float = Field(description="Wall-clock time the wait took")


def _get_process_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _ignore_output_line(line: str, is_stdout: bool) -> None:
    pass


def measure_cpu_waiting_on_idle_children(child_script: str, child_count: int) -> IdleChildrenCpuMeasurement:
    """Run ``child_count`` copies of ``child_script`` in the background and measure this process's CPU until they exit.

    Counts every thread in the calling process, so run it in a fresh interpreter to keep other work out of the number.
    """
    with ConcurrencyGroup(name="idle_children") as cg:
        processes = [
            cg.run_process_in_background(
                ["sh", "-c", child_script],
                on_output=_ignore_output_line,
                is_output_accumulated=False,
            )
            for _ in range(child_count)
        ]
        cpu_seconds_before = _get_process_cpu_seconds()
        wall_seconds_before = time.monotonic()
        for process in processes:
            process.wait(timeout=30.0)
        cpu_seconds_used = _get_process_cpu_seconds() - cpu_seconds_before
        wall_seconds_elapsed = time.monotonic() - wall_seconds_before
    assert all(process.returncode == 0 for process in processes)
    return IdleChildrenCpuMeasurement(cpu_seconds=cpu_seconds_used, wall_seconds=wall_seconds_elapsed)
