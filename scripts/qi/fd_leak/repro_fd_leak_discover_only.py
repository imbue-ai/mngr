"""Repro: test discover_hosts_and_agents for each provider individually.

Usage:
    uv run python scripts/qi/repro_fd_leak_discover_only.py
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
            fd = int(entry.name)
            os.fstat(fd)
            count += 1
        except (ValueError, OSError):
            pass
    return count


def main() -> None:
    gc.collect()
    initial = count_real_fds()
    print(f"Initial real FDs: {initial}")

    cg = ConcurrencyGroup(name="repro")
    with cg:
        pm = create_plugin_manager()
        mngr_ctx = load_config(pm, cg)

        print("\n--- Test 1: discover local only ---")
        base = count_real_fds()
        for i in range(1, 6):
            discover_hosts_and_agents(
                mngr_ctx, provider_names=("local",), agent_identifiers=None, include_destroyed=True, reset_caches=False
            )
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - base:+d})")

        print("\n--- Test 2: discover modal only ---")
        base = count_real_fds()
        for i in range(1, 6):
            discover_hosts_and_agents(
                mngr_ctx, provider_names=("modal",), agent_identifiers=None, include_destroyed=True, reset_caches=False
            )
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - base:+d})")

        print("\n--- Test 3: discover both ---")
        base = count_real_fds()
        for i in range(1, 6):
            discover_hosts_and_agents(
                mngr_ctx, provider_names=None, agent_identifiers=None, include_destroyed=True, reset_caches=False
            )
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - base:+d})")


if __name__ == "__main__":
    main()
