"""Minimal repro: grpclib leaks sockets when used from parallel threads.

When grpclib channels are used from threads with different asyncio event
loops, each thread creates new TCP connections that are never closed.
This happens because grpclib.client.Channel stores a single _protocol
bound to one event loop, but Modal's synchronize_api creates a new loop
per thread.

The leak only manifests when multiple threads use grpclib concurrently
(e.g. local + modal providers discovering in parallel). Running a single
provider at a time does not leak.

Usage:
    uv run python scripts/qi/fd_leak/repro_grpclib_fd_leak.py [--iterations N]
"""

import argparse
import gc
import os
import threading
from pathlib import Path

import modal


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


def modal_api_call(app_id: str, volume: modal.Volume) -> None:
    """Make Modal SDK calls (uses grpclib internally)."""
    list(modal.Sandbox.list(app_id=app_id))
    try:
        list(volume.listdir("/hosts/"))
    except Exception:
        pass


def busy_work() -> None:
    """Non-Modal work in a parallel thread (simulates local provider)."""
    import time

    time.sleep(0.1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--app-name", type=str, default="mngr-modal")
    parser.add_argument("--volume-name", type=str, default="mngr-modal-state")
    parser.add_argument("--environment", type=str, default="mngr-Qi")
    args = parser.parse_args()

    app = modal.App.lookup(args.app_name, create_if_missing=False, environment_name=args.environment)
    volume = modal.Volume.from_name(args.volume_name, environment_name=args.environment, version=2)
    app_id = app.app_id

    gc.collect()
    initial = count_real_fds()
    print(f"Initial FDs: {initial}")

    print("\n--- Test 1: Modal API calls from a single thread (no leak expected) ---")
    base = count_real_fds()
    for i in range(1, args.iterations + 1):
        t = threading.Thread(target=modal_api_call, args=(app_id, volume))
        t.start()
        t.join()
        gc.collect()
        current = count_real_fds()
        print(f"[{i:3d}] FDs: {current} (delta: {current - base:+d})")

    print("\n--- Test 2: Modal API calls with a parallel busy thread (leak expected) ---")
    base = count_real_fds()
    for i in range(1, args.iterations + 1):
        t1 = threading.Thread(target=modal_api_call, args=(app_id, volume))
        t2 = threading.Thread(target=busy_work)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        gc.collect()
        current = count_real_fds()
        print(f"[{i:3d}] FDs: {current} (delta: {current - base:+d})")


if __name__ == "__main__":
    main()
