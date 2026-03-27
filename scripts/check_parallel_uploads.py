"""Janky script to test parallel file uploads to a Modal host.

Tests whether running multiple host.write_file() calls in parallel (from
different threads) causes hangs, and whether the mitigation of using
separate paramiko SFTP channels per thread works.

RESULT (before mitigation): parallel write_file hangs because pyinfra
uses a single memoized SFTPClient (channel) for all uploads, and paramiko
channels are not thread-safe.

MITIGATION: Create a new SFTPClient per thread from the shared paramiko
Transport (which IS thread-safe). Each thread gets its own SFTP channel.

Usage:
    PYTHONUNBUFFERED=1 uv run python scripts/check_parallel_uploads.py
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import as_completed
from pathlib import Path
from threading import current_thread

import pluggy

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.agents.agent_registry import load_agents_from_plugins
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins import hookspecs
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.registry import load_backends_from_plugins

HOST_NAME = HostName("spica")
NUM_FILES = 10
FILE_SIZE_BYTES = 1024  # 1 KB of random data per file
PARALLEL_TIMEOUT_SECONDS = 30


def get_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Find the 'spica' host and return it."""
    print("  Discovering hosts (modal only)...")
    agents_by_host, _providers = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=("modal",),
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    print(f"  Discovery complete. Found {len(agents_by_host)} host(s).")

    for host_ref in agents_by_host:
        if host_ref.host_name == HOST_NAME:
            print(f"  Found target host ({host_ref.host_id}). Connecting...")
            provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
            host = provider.get_host(host_ref.host_id)
            if not isinstance(host, OnlineHostInterface):
                raise RuntimeError(f"Host {HOST_NAME} is not online")
            return host

    raise RuntimeError(
        f"Host '{HOST_NAME}' not found. Available hosts: " + ", ".join(str(h.host_name) for h in agents_by_host)
    )


def write_file_via_pyinfra(host: OnlineHostInterface, file_index: int) -> tuple[int, float, str]:
    """Write using host.write_file() (the normal path -- hangs in parallel)."""
    path = Path(f"/tmp/parallel_upload_test_{file_index}.bin")
    content = os.urandom(FILE_SIZE_BYTES)
    thread_name = current_thread().name

    print(f"  [{thread_name}] Starting upload #{file_index} -> {path}")
    start = time.monotonic()
    try:
        host.write_file(path=path, content=content)
        elapsed = time.monotonic() - start
        print(f"  [{thread_name}] Finished upload #{file_index} in {elapsed:.2f}s")
        return (file_index, elapsed, "ok")
    except Exception as e:
        elapsed = time.monotonic() - start
        print(f"  [{thread_name}] FAILED upload #{file_index} after {elapsed:.2f}s: {e}")
        return (file_index, elapsed, f"error: {e}")


def run_sequential(host: OnlineHostInterface) -> None:
    """Run uploads sequentially as a baseline."""
    print(f"\n--- Sequential uploads ({NUM_FILES} files) ---")
    start = time.monotonic()
    results = []
    for i in range(NUM_FILES):
        results.append(write_file_via_pyinfra(host, i))
    total = time.monotonic() - start
    print(f"\nSequential total: {total:.2f}s")
    for idx, elapsed, status in results:
        print(f"  File {idx}: {elapsed:.2f}s [{status}]")


def run_parallel(label: str, fn, fn_arg, max_workers: int) -> bool:
    """Run uploads in parallel. Detects hangs via timeout. Returns True if hung."""
    print(f"\n--- {label} ({NUM_FILES} files, {max_workers} workers, timeout={PARALLEL_TIMEOUT_SECONDS}s) ---")
    start = time.monotonic()
    results: list[tuple[int, float, str]] = []

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(fn, fn_arg, i): i for i in range(NUM_FILES)}
    try:
        for future in as_completed(futures, timeout=PARALLEL_TIMEOUT_SECONDS):
            results.append(future.result())
    except FuturesTimeoutError:
        total = time.monotonic() - start
        completed = len(results)
        pending = NUM_FILES - completed
        print(f"\n  HUNG! Timed out after {total:.2f}s. {completed}/{NUM_FILES} completed, {pending} still pending.")
        executor.shutdown(wait=False, cancel_futures=True)
        return True

    executor.shutdown(wait=True)
    total = time.monotonic() - start
    results.sort(key=lambda r: r[0])
    print(f"\nTotal: {total:.2f}s")
    for idx, elapsed, status in results:
        print(f"  File {idx}: {elapsed:.2f}s [{status}]")
    return False


def main() -> None:
    print("=== Parallel Upload Test ===")
    print(f"Target host: {HOST_NAME}")
    print(f"Files: {NUM_FILES} x {FILE_SIZE_BYTES} bytes each")

    # Bootstrap mngr context
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.load_setuptools_entrypoints("mngr")
    load_backends_from_plugins(pm)
    load_agents_from_plugins(pm)

    cg = ConcurrencyGroup(name="parallel-upload-test")
    with cg:
        mngr_ctx = load_config(pm, cg)

        print("\nResolving host...")
        host = get_host(mngr_ctx)
        print(f"Found host: {host.get_name()} (id={host.id})")

        # Sanity check
        print("\n--- Single upload sanity check ---")
        idx, elapsed, status = write_file_via_pyinfra(host, 999)
        if status != "ok":
            print(f"Single upload failed: {status}")
            sys.exit(1)
        print(f"Single upload OK ({elapsed:.2f}s)")

        # Sequential baseline
        run_sequential(host)

        # Test: Parallel via host.write_file() (now fixed to use separate
        # paramiko SFTP channels per call instead of pyinfra's memoized one)
        is_hung = run_parallel(
            "Parallel via host.write_file() (fixed)",
            write_file_via_pyinfra,
            host,
            max_workers=5,
        )
        if is_hung:
            print("\n  FAILED: parallel host.write_file() still hangs.")
            sys.exit(1)
        else:
            print("\n  SUCCESS: parallel host.write_file() works.")

        # Cleanup
        print("\n--- Cleanup ---")
        for i in list(range(NUM_FILES)) + [999]:
            path = Path(f"/tmp/parallel_upload_test_{i}.bin")
            host.execute_idempotent_command(f"rm -f {path}")
        print("Done.")


if __name__ == "__main__":
    main()
