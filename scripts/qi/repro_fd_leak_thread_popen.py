"""Bisect: is the leak from Popen in a thread that then exits?

Usage:
    uv run python scripts/qi/repro_fd_leak_thread_popen.py
"""

import gc
import os
import subprocess
import threading
from pathlib import Path

from pyinfra.api.command import StringCommand
from pyinfra.api.inventory import Inventory
from pyinfra.api.state import State


def count_real_fds() -> int:
    count = 0
    for entry in Path("/dev/fd").iterdir():
        try:
            fd = int(entry.name)
            os.fstat(fd)
            count += 1
        except (ValueError, OSError):
            pass
    return count


def run_pyinfra_in_thread() -> None:
    """Create pyinfra host, run command, disconnect -- all in a thread."""
    names_data = (["@local"], {})
    inventory = Inventory(names_data)
    state = State(inventory=inventory)
    host = inventory.get_host("@local")
    host.init(state)
    if not host.connected:
        host.connect(raise_exceptions=True)
    host.run_shell_command(StringCommand("echo", "hello"))
    host.disconnect()


def run_popen_in_thread() -> None:
    """Create Popen in a thread, wait, close -- all in a thread."""
    p = subprocess.Popen(
        "echo hello", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE
    )
    assert p.stdin is not None and p.stdout is not None and p.stderr is not None
    p.stdin.close()
    p.wait()
    p.stdout.close()
    p.stderr.close()


def main() -> None:
    gc.collect()
    initial = count_real_fds()
    print(f"Initial real FDs: {initial}")

    print("\n--- Test 1: pyinfra local command in thread ---")
    for i in range(1, 11):
        t = threading.Thread(target=run_pyinfra_in_thread)
        t.start()
        t.join()
        gc.collect()
        current = count_real_fds()
        print(f"[{i:3d}] real_fds={current} (delta: {current - initial:+d})")

    print("\n--- Test 2: raw Popen in thread ---")
    mid = count_real_fds()
    for i in range(1, 11):
        t = threading.Thread(target=run_popen_in_thread)
        t.start()
        t.join()
        gc.collect()
        current = count_real_fds()
        print(f"[{i:3d}] real_fds={current} (delta: {current - mid:+d})")


if __name__ == "__main__":
    main()
