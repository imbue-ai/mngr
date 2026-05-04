"""Isolation step 2: Run providers sequentially (no ConcurrencyGroupExecutor).

Calls each provider's discover_hosts_and_agents directly on the main thread.
If this does NOT leak, the issue is in the parallel execution.

Usage:
    uv run python scripts/qi/fd_leak/isolate_02_sequential.py
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
        providers = get_all_provider_instances(mngr_ctx, None)

        gc.collect()
        base = count_real_fds()
        print(f"Baseline FDs: {base}")
        print(f"Providers: {[p.name for p in providers]}")

        for i in range(1, 6):
            for provider in providers:
                provider.discover_hosts_and_agents(
                    cg=mngr_ctx.concurrency_group,
                    include_destroyed=True,
                )
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - base:+d})")


if __name__ == "__main__":
    main()
