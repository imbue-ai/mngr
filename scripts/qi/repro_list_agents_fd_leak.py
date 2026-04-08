"""Repro script: call list_agents repeatedly and monitor FD count.

Usage:
    uv run python scripts/qi/repro_list_agents_fd_leak.py [--interval SECS] [--iterations N]
"""

import argparse
import os
import stat
import time
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.primitives import ErrorBehavior


def count_open_fds() -> int:
    fd_dir = Path("/dev/fd")
    try:
        return len(list(fd_dir.iterdir()))
    except OSError:
        return -1


def describe_new_fds(baseline_fds: set[int]) -> str:
    """Describe FDs that are open now but weren't in the baseline."""
    current_fds: set[int] = set()
    for entry in Path("/dev/fd").iterdir():
        try:
            current_fds.add(int(entry.name))
        except ValueError:
            pass

    new_fds = sorted(current_fds - baseline_fds)
    if not new_fds:
        return "no new FDs"

    descriptions = []
    for fd in new_fds[:10]:
        try:
            st = os.fstat(fd)
            if stat.S_ISFIFO(st.st_mode):
                desc = "pipe"
            elif stat.S_ISSOCK(st.st_mode):
                desc = "socket"
            elif stat.S_ISREG(st.st_mode):
                desc = "file"
            elif stat.S_ISCHR(st.st_mode):
                desc = "chr"
            else:
                desc = f"mode={oct(st.st_mode)}"
        except OSError:
            desc = "closed?"
        descriptions.append(f"{fd}={desc}")

    suffix = f" (+{len(new_fds) - 10} more)" if len(new_fds) > 10 else ""
    return ", ".join(descriptions) + suffix


def get_fd_set() -> set[int]:
    fds: set[int] = set()
    for entry in Path("/dev/fd").iterdir():
        try:
            fds.add(int(entry.name))
        except ValueError:
            pass
    return fds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()

    cg = ConcurrencyGroup(name="repro")
    with cg:
        pm = create_plugin_manager()
        mngr_ctx = load_config(pm, cg)

        initial_fds = count_open_fds()
        baseline = get_fd_set()
        print(f"Initial FDs: {initial_fds}")

        for i in range(1, args.iterations + 1):
            pre_fds = get_fd_set()
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
            new_this_iter = describe_new_fds(pre_fds)
            print(f"[{i:3d}] FDs: {current_fds} (delta: {delta:+d}, agents: {agent_count})  new: {new_this_iter}")

            if i < args.iterations:
                time.sleep(args.interval)

    final_fds = count_open_fds()
    print(f"\nFinal FDs: {final_fds} (total delta: {final_fds - initial_fds:+d})")
    print(f"All leaked: {describe_new_fds(baseline)}")


if __name__ == "__main__":
    main()
