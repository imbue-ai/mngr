"""SessionStart hook: label-driven reap of terminal subagents.

Identical behavior for PROXY and DENY modes -- both spawn paths attach
``mngr_claude_subagent_proxy_parent_id=${MNGR_AGENT_ID}`` to every
child, so the same label query identifies orphans regardless of mode.
PROXY mode's per-tool_use_id sidefiles under ``subagent_map/`` etc.
get cleaned up alongside the destroy step; DENY mode never writes
those sidefiles, so the cleanup pass is a no-op there.

Plugin-cache Stop-hook guarding (PROXY's other historical SessionStart
concern) lives in a separate hook (``hooks/guard_stop_hooks.py``) so
this module stays literally shared across both modes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable
from typing import TextIO

from loguru import logger

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_claude_subagent_proxy.hooks.destroy_detached import DestroyAgentDetachedCallable
from imbue.mngr_claude_subagent_proxy.hooks.destroy_detached import destroy_agent_detached
from imbue.mngr_claude_subagent_proxy.hooks.mngr_api import ListAgentsByNameCallable
from imbue.mngr_claude_subagent_proxy.hooks.mngr_api import find_terminal_children
from imbue.mngr_claude_subagent_proxy.hooks.mngr_api import list_agents_by_name

# Stub-injection alias for the background reaper spawner. Lives here because
# this is the only caller.
SpawnBackgroundReaperCallable = Callable[[], None]


def _map_files(state_dir: Path) -> list[Path]:
    """Return sorted list of subagent_map/*.json files, or [] if dir missing.

    Empty list in DENY mode (no map dir is ever written), so the
    sidefile-cleanup loop is a no-op there.
    """
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
        state_dir / "proxy_commands" / f"watermark-{tid}",
    ]
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("reap: failed to remove {}: {}", path, e)


def _target_name_for_map_file(map_file: Path) -> str:
    """Extract ``target_name`` from a subagent_map entry, or '' on any failure."""
    try:
        map_data = json.loads(map_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("reap: failed to read {}: {}", map_file, e)
        return ""
    if not isinstance(map_data, dict):
        return ""
    raw_target = map_data.get("target_name")
    if not isinstance(raw_target, str):
        return ""
    return raw_target


def _cleanup_stale_sidefiles(
    state_dir: Path,
    agents_by_name: dict[str, AgentDetails],
    destroyed_names: set[str],
) -> None:
    """Drop per-tid sidefiles whose target was just destroyed or has vanished.

    No-op in DENY mode (no subagent_map dir to iterate). In PROXY mode
    a tid whose target is still RUNNING / WAITING keeps its sidefiles
    (the Task is in flight), but a tid whose target was destroyed this
    iteration or whose target no longer exists in ``mngr list`` is
    safe to clear.
    """
    for map_file in _map_files(state_dir):
        tid = map_file.stem
        if not tid:
            continue
        target_name = _target_name_for_map_file(map_file)
        if not target_name:
            _cleanup_tid(state_dir, tid)
            continue
        if target_name in destroyed_names or target_name not in agents_by_name:
            _cleanup_tid(state_dir, tid)


def _do_reap(
    state_dir: Path,
    list_callable: ListAgentsByNameCallable,
    destroy_callable: DestroyAgentDetachedCallable,
) -> None:
    """Synchronous reaper body; runs detached from the SessionStart hook.

    Label-driven destroy + (PROXY-only) sidefile cleanup. DENY mode
    runs this same code; the sidefile cleanup is a no-op there.

    The two halves are independent: a missing ``MNGR_AGENT_ID`` skips
    the label-driven destroy (we don't know which children are ours),
    but stale sidefiles can still be cleaned based on whether their
    recorded target_name is present in ``mngr list``.
    """
    agents_by_name = list_callable()
    if agents_by_name is None:
        return

    parent_id = os.environ.get("MNGR_AGENT_ID", "")
    destroyed_names: set[str] = set()
    if parent_id:
        terminals = find_terminal_children(parent_id, agents_by_name)
        if terminals:
            destroy_log = state_dir / "subagent_destroy.log"
            logger.info(
                "reap: parent {} has {} terminal child(ren) to reap",
                parent_id,
                len(terminals),
            )
            for child in terminals:
                destroy_callable(child.name, destroy_log)
                destroyed_names.add(child.name)
    else:
        logger.warning("reap: MNGR_AGENT_ID unset; skipping label-driven destroy")

    _cleanup_stale_sidefiles(state_dir, agents_by_name, destroyed_names)


def spawn_background_reaper() -> None:
    """Re-invoke this module with MNGR_SUBAGENT_REAP_BACKGROUND=1 in a detached session."""
    env = os.environ.copy()
    env["MNGR_SUBAGENT_REAP_BACKGROUND"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "imbue.mngr_claude_subagent_proxy.hooks.reap"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as e:
        logger.warning("reap: failed to launch background reaper: {}", e)


def run(
    stdin: TextIO,
    list_callable: ListAgentsByNameCallable = list_agents_by_name,
    destroy_callable: DestroyAgentDetachedCallable = destroy_agent_detached,
    spawn_background_callable: SpawnBackgroundReaperCallable = spawn_background_reaper,
) -> None:
    """SessionStart hook core.

    Always dispatches a background child to do the slow ``mngr list``
    + destroy work, so the SessionStart hook itself returns immediately.
    Same behavior in PROXY and DENY modes.
    """
    # 0o077 keeps any files written downstream (notably the
    # subagent_destroy.log opened by destroy_agent_detached in the
    # background-worker branch) at 0600. Matches hooks/cleanup.py and
    # hooks/spawn.py, which set the same umask at the top of their run().
    os.umask(0o077)
    try:
        stdin.read()
    except OSError:
        pass

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir_env:
        return
    state_dir = Path(state_dir_env)

    is_background_worker = os.environ.get("MNGR_SUBAGENT_REAP_BACKGROUND") == "1"
    if is_background_worker:
        _do_reap(state_dir, list_callable, destroy_callable)
        return

    spawn_background_callable()


def main() -> None:
    """SessionStart hook entry point. Wires up the real stdin and helpers."""
    run(sys.stdin)


if __name__ == "__main__":
    main()
