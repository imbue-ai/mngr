"""Isolation step 5: One real provider + one noop in parallel.

Tests each provider in the executor alongside a noop thread. This determines
whether the leak requires both providers to do real work, or just one provider
running in a threaded executor is enough.

Usage:
    uv run python scripts/qi/fd_leak/isolate_05_parallel_one_real_one_noop.py
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


def noop() -> None:
    pass


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
        provider_map = {p.name: p for p in providers}

        gc.collect()
        base = count_real_fds()
        print(f"Baseline FDs: {base}")
        print(f"Providers: {list(provider_map.keys())}")

        for provider_name, provider in provider_map.items():
            print(f"\n--- {provider_name} + noop in parallel ---")
            test_base = count_real_fds()
            for i in range(1, 6):
                results: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
                lock = Lock()
                with ConcurrencyGroupExecutor(
                    parent_cg=mngr_ctx.concurrency_group,
                    name=f"test_{provider_name}",
                    max_workers=32,
                ) as executor:
                    executor.submit(
                        discover_one_provider,
                        provider,
                        results,
                        lock,
                        mngr_ctx.concurrency_group,
                    )
                    executor.submit(noop)
                gc.collect()
                current = count_real_fds()
                print(f"[{i}] FDs: {current} (delta: {current - test_base:+d})")


if __name__ == "__main__":
    main()
