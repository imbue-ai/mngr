"""Isolation step 3: Run providers in ConcurrencyGroupExecutor but with noop work.

Submits noop functions to the executor instead of actual provider discovery.
If this does NOT leak, the issue is in what the providers do, not the executor.

Usage:
    uv run python scripts/qi/fd_leak/isolate_03_parallel_noop.py
"""

import gc
import os
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import create_plugin_manager


def count_real_fds() -> int:
    count = 0
    for entry in Path("/dev/fd").iterdir():
        try:
            os.fstat(int(entry.name))
            count += 1
        except (ValueError, OSError):
            pass
    return count


def noop() -> None:
    pass


def main() -> None:
    cg = ConcurrencyGroup(name="repro")
    with cg:
        pm = create_plugin_manager()
        mngr_ctx = load_config(pm, cg)

        gc.collect()
        base = count_real_fds()
        print(f"Baseline FDs: {base}")

        for i in range(1, 6):
            with ConcurrencyGroupExecutor(
                parent_cg=mngr_ctx.concurrency_group,
                name="test_executor",
                max_workers=32,
            ) as executor:
                executor.submit(noop)
                executor.submit(noop)
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - base:+d})")


if __name__ == "__main__":
    main()
