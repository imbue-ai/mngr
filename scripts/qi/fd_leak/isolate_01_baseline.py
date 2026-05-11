"""Isolation step 1: Confirm the leak exists in discover_hosts_and_agents.

This is the baseline -- calls discover_hosts_and_agents with all providers
and checks for FD growth.

Usage:
    uv run python scripts/qi/fd_leak/isolate_01_baseline.py
"""

import gc
import os
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.discover import discover_hosts_and_agents
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


def main() -> None:
    cg = ConcurrencyGroup(name="repro")
    with cg:
        pm = create_plugin_manager()
        mngr_ctx = load_config(pm, cg)

        gc.collect()
        base = count_real_fds()
        print(f"Baseline FDs: {base}")

        for i in range(1, 6):
            discover_hosts_and_agents(
                mngr_ctx,
                provider_names=None,
                agent_identifiers=None,
                include_destroyed=True,
                reset_caches=False,
            )
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - base:+d})")


if __name__ == "__main__":
    main()
