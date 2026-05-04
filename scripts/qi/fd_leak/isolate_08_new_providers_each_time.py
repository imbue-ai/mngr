"""Isolation step 8: Create new provider instances each iteration.

Tests whether calling get_all_provider_instances() each time (creating new
provider instances) is what causes the FD leak. Compare against reusing
the same providers.

Usage:
    uv run python scripts/qi/fd_leak/isolate_08_new_providers_each_time.py
"""

import gc
import os
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.providers import get_all_provider_instances
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

        print("\n--- Test 1: Reuse same providers (no leak expected) ---")
        providers = get_all_provider_instances(mngr_ctx, None)
        test_base = count_real_fds()
        for i in range(1, 6):
            for p in providers:
                p.discover_hosts_and_agents(cg=mngr_ctx.concurrency_group, include_destroyed=True)
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - test_base:+d})")

        print("\n--- Test 2: Create new providers each time (leak expected) ---")
        test_base = count_real_fds()
        for i in range(1, 6):
            fresh_providers = get_all_provider_instances(mngr_ctx, None)
            for p in fresh_providers:
                p.discover_hosts_and_agents(cg=mngr_ctx.concurrency_group, include_destroyed=True)
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - test_base:+d})")

        print("\n--- Test 3: Create new providers each time but DON'T discover ---")
        test_base = count_real_fds()
        for i in range(1, 6):
            _fresh = get_all_provider_instances(mngr_ctx, None)
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - test_base:+d})")


if __name__ == "__main__":
    main()
