"""Isolation step 4: Run real provider discovery in ConcurrencyGroupExecutor.

Reimplements _run_discovery inline to match the exact structure but without
using the mngr discover module. This confirms the leak is in the parallel
provider discovery pattern.

Usage:
    uv run python scripts/qi/fd_leak/isolate_04_parallel_real_providers.py
"""

import gc
import os
from pathlib import Path
from threading import Lock

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.providers.base_provider import BaseProviderInstance


def count_real_fds() -> int:
    count = 0
    for entry in Path("/dev/fd").iterdir():
        try:
            os.fstat(int(entry.name))
            count += 1
        except (ValueError, OSError):
            pass
    return count


def discover_one_provider(
    provider: BaseProviderInstance,
    results: dict[DiscoveredHost, list[DiscoveredAgent]],
    lock: Lock,
    cg: ConcurrencyGroup,
) -> None:
    provider_results = provider.discover_hosts_and_agents(cg=cg, include_destroyed=True)
    with lock:
        results.update(provider_results)


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
            results: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
            lock = Lock()
            with ConcurrencyGroupExecutor(
                parent_cg=mngr_ctx.concurrency_group,
                name="discover_hosts_and_agents",
                max_workers=32,
            ) as executor:
                for provider in providers:
                    executor.submit(
                        discover_one_provider,
                        provider,
                        results,
                        lock,
                        mngr_ctx.concurrency_group,
                    )
            gc.collect()
            current = count_real_fds()
            print(f"[{i}] FDs: {current} (delta: {current - base:+d})")


if __name__ == "__main__":
    main()
