"""Repro script: call list_agents repeatedly and monitor FD count.

Usage:
    uv run python scripts/qi/repro_list_agents_fd_leak.py [--interval SECS] [--iterations N]
"""

import argparse
import os
import time
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.loader import load_mngr_context
from imbue.mngr.primitives import ErrorBehavior


def count_open_fds() -> int:
    fd_dir = Path("/dev/fd")
    if not fd_dir.exists():
        fd_dir = Path(f"/proc/{os.getpid()}/fd")
    try:
        return len(list(fd_dir.iterdir()))
    except OSError:
        return -1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()

    cg = ConcurrencyGroup(name="repro")
    with cg:
        mngr_ctx = load_mngr_context(concurrency_group=cg)

        initial_fds = count_open_fds()
        print(f"Initial FDs: {initial_fds}")

        for i in range(1, args.iterations + 1):
            try:
                result = list_agents(
                    mngr_ctx=mngr_ctx,
                    is_streaming=False,
                    error_behavior=ErrorBehavior.CONTINUE,
                )
                agent_count = len(result.agents) if result else 0
            except Exception as exc:
                agent_count = -1
                print(f"  Error: {exc}")

            current_fds = count_open_fds()
            delta = current_fds - initial_fds
            print(f"[{i:3d}] FDs: {current_fds} (delta: {delta:+d}, agents: {agent_count})")

            if i < args.iterations:
                time.sleep(args.interval)

    final_fds = count_open_fds()
    print(f"\nFinal FDs: {final_fds} (total delta: {final_fds - initial_fds:+d})")


if __name__ == "__main__":
    main()
