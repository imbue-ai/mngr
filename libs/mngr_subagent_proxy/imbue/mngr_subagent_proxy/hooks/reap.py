"""SessionStart hook. Cleans up per-tool_use_id state files left behind when
a previous parent session died mid-subagent, and destroys any lingering
mngr proxy agents in a terminal lifecycle state. Fast-path exits when the
per-session state dir is empty; otherwise backgrounds the cleanup work.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_subagent_proxy.hooks.mngr_api import destroy_agent_detached
from imbue.mngr_subagent_proxy.hooks.mngr_api import list_agents_by_name

_TERMINAL_STATES: frozenset[AgentLifecycleState] = frozenset({AgentLifecycleState.DONE, AgentLifecycleState.STOPPED})


def _map_files(state_dir: Path) -> list[Path]:
    """Return sorted list of subagent_map/*.json files, or [] if dir missing."""
    map_dir = state_dir / "subagent_map"
    if not map_dir.is_dir():
        return []
    return sorted(p for p in map_dir.glob("*.json") if p.is_file())


def _cleanup_tid(state_dir: Path, tid: str) -> None:
    """Remove all side files for tool_use_id `tid` under state_dir, best-effort."""
    paths = [
        state_dir / "proxy_commands" / f"env-{tid}.env",
        state_dir / "subagent_map" / f"{tid}.json",
        state_dir / "subagent_prompts" / f"{tid}.md",
        state_dir / "subagent_results" / f"{tid}.txt",
        state_dir / "proxy_commands" / f"wait-{tid}.sh",
        state_dir / "proxy_commands" / f"initialized-{tid}",
    ]
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("reap: failed to remove {}: {}", path, e)


def _process_map_file(state_dir: Path, map_file: Path, agents_by_name: dict[str, AgentDetails]) -> None:
    """Inspect a single subagent_map entry and reap if its target is gone or terminal."""
    tid = map_file.stem
    if not tid:
        return

    try:
        map_data = json.loads(map_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("reap: failed to read {}: {}", map_file, e)
        _cleanup_tid(state_dir, tid)
        return

    target_name = ""
    if isinstance(map_data, dict):
        raw_target = map_data.get("target_name")
        if isinstance(raw_target, str):
            target_name = raw_target

    if not target_name:
        _cleanup_tid(state_dir, tid)
        return

    agent_details = agents_by_name.get(target_name)
    if agent_details is None:
        # Agent no longer exists; drop the side files.
        _cleanup_tid(state_dir, tid)
        return

    if agent_details.state in _TERMINAL_STATES:
        destroy_log = state_dir / "subagent_destroy.log"
        destroy_agent_detached(target_name, destroy_log)
        _cleanup_tid(state_dir, tid)


def _do_reap(state_dir: Path) -> None:
    """Synchronous reaper body; intended to run detached from the hook invocation."""
    agents_map = list_agents_by_name()
    if agents_map is None:
        return
    for map_file in _map_files(state_dir):
        _process_map_file(state_dir, map_file, agents_map)


def spawn_background_reaper() -> None:
    """Re-invoke this module with MNGR_SUBAGENT_REAP_BACKGROUND=1 in a detached session."""
    env = os.environ.copy()
    env["MNGR_SUBAGENT_REAP_BACKGROUND"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "imbue.mngr_subagent_proxy.hooks.reap"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as e:
        logger.warning("reap: failed to launch background reaper: {}", e)


def main() -> None:
    """SessionStart hook entry point."""
    os.umask(0o077)
    try:
        sys.stdin.read()
    except OSError:
        pass

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir_env:
        return
    state_dir = Path(state_dir_env)

    is_background_worker = os.environ.get("MNGR_SUBAGENT_REAP_BACKGROUND") == "1"
    if is_background_worker:
        _do_reap(state_dir)
        return

    # Fast path: nothing to reap.
    if not _map_files(state_dir):
        return

    # There is work to do; do it in a detached child so the SessionStart hook
    # returns immediately.
    spawn_background_reaper()


if __name__ == "__main__":
    main()
