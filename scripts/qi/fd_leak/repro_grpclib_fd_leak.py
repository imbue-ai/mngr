"""Repro: socket leak when multiple providers discover in parallel.

The leak occurs only when local + modal providers run in the same
ConcurrencyGroupExecutor. Running either provider alone is stable.

This script exercises the same code path as list_agents but isolates the
discover phase to show the leak more clearly. It also tests sequential
discovery (no executor) to confirm the leak is specific to parallel execution.

Usage:
    uv run python scripts/qi/fd_leak/repro_grpclib_fd_leak.py [--iterations N]
"""

import argparse
import gc
import os
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.providers import get_all_provider_instances
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args()

    gc.collect()
    initial = count_real_fds()
    print(f"Initial real FDs: {initial}")

    cg = ConcurrencyGroup(name="repro")
    with cg:
        pm = create_plugin_manager()
        mngr_ctx = load_config(pm, cg)

        print("\n--- Test 1: discover both providers in parallel (via ConcurrencyGroupExecutor) ---")
        base = count_real_fds()
        for i in range(1, args.iterations + 1):
            discover_hosts_and_agents(
                mngr_ctx, provider_names=None, agent_identifiers=None, include_destroyed=True, reset_caches=False
            )
            gc.collect()
            current = count_real_fds()
            print(f"[{i:3d}] FDs: {current} (delta: {current - base:+d})")

        print("\n--- Test 2: discover both providers sequentially (no executor, no leak) ---")
        providers = get_all_provider_instances(mngr_ctx, None)
        base = count_real_fds()
        for i in range(1, args.iterations + 1):
            for provider in providers:
                provider.discover_hosts_and_agents(cg=mngr_ctx.concurrency_group, include_destroyed=True)
            gc.collect()
            current = count_real_fds()
            print(f"[{i:3d}] FDs: {current} (delta: {current - base:+d})")


if __name__ == "__main__":
    main()
