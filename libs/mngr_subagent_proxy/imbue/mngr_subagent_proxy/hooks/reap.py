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

_TERMINAL_STATES: frozenset[str] = frozenset({"DONE", "STOPPED", "FAILED", "DESTROYED", "TERMINATED"})


def _map_files(state_dir: Path) -> list[Path]:
    """Return sorted list of subagent_map/*.json files, or [] if dir missing."""
    map_dir = state_dir / "subagent_map"
    if not map_dir.is_dir():
        return []
    return sorted(p for p in map_dir.glob("*.json") if p.is_file())


def _list_agents_via_subprocess() -> list[dict[str, object]]:
    """Invoke `uv run mngr list --format json` and return the agents list.

    Returns [] on any failure.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["uv", "run", "mngr", "list", "--format", "json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("reap: mngr list failed: {}", e)
        return []
    if completed.returncode != 0:
        logger.warning("reap: mngr list exited {}: {}", completed.returncode, completed.stderr)
        return []
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as e:
        logger.warning("reap: mngr list returned invalid JSON: {}", e)
        return []
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        agents = payload.get("agents")
        if isinstance(agents, list):
            return [entry for entry in agents if isinstance(entry, dict)]
    return []


def _lookup_lifecycle_state(agents: list[dict[str, object]], target_name: str) -> str | None:
    """Return the upper-cased lifecycle_state/state/status for target_name, or None if absent."""
    for agent in agents:
        name = agent.get("name")
        if name != target_name:
            continue
        for key in ("lifecycle_state", "state", "status"):
            value = agent.get(key)
            if isinstance(value, str) and value:
                return value.upper()
        return ""
    return None


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


def _shell_destroy_detached(target_name: str, destroy_log: Path) -> None:
    """Fire-and-forget `uv run mngr destroy <target> --yes`."""
    try:
        log_handle = destroy_log.open("ab")
    except OSError as e:
        logger.warning("reap: failed to open destroy log {}: {}", destroy_log, e)
        log_handle = None
    try:
        subprocess.Popen(  # noqa: S603
            ["uv", "run", "mngr", "destroy", target_name, "--yes"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_handle if log_handle is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        logger.warning("reap: failed to launch mngr destroy: {}", e)
    finally:
        if log_handle is not None:
            log_handle.close()


def _process_map_file(state_dir: Path, map_file: Path, agents: list[dict[str, object]]) -> None:
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

    lifecycle = _lookup_lifecycle_state(agents, target_name)
    if lifecycle is None:
        # Agent no longer exists; drop the side files.
        _cleanup_tid(state_dir, tid)
        return

    if lifecycle in _TERMINAL_STATES:
        destroy_log = state_dir / "subagent_destroy.log"
        _shell_destroy_detached(target_name, destroy_log)
        _cleanup_tid(state_dir, tid)


def _do_reap(state_dir: Path) -> None:
    """Synchronous reaper body; intended to run detached from the hook invocation."""
    agents = _list_agents_via_subprocess()
    for map_file in _map_files(state_dir):
        _process_map_file(state_dir, map_file, agents)


def _background_reap(state_dir: Path) -> None:
    """Re-invoke this module with MNGR_SUBAGENT_REAP_BACKGROUND=1 in a detached session."""
    env = os.environ.copy()
    env["MNGR_SUBAGENT_REAP_BACKGROUND"] = "1"
    try:
        subprocess.Popen(  # noqa: S603
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
    _background_reap(state_dir)


if __name__ == "__main__":
    main()
