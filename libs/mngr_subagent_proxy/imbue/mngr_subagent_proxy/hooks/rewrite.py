"""PostToolUse:Agent hook. Tears down the mngr subagent and cleans up
per-tool_use_id state files after the Task tool returns.

The subagent's actual end-turn text reaches the parent via Haiku's own
final reply (the wait-script prints the text to Haiku's stdout and
Haiku is instructed to echo it verbatim) -- not via this hook, because
Claude Code's PostToolUse ``updatedToolOutput`` field is MCP-only and
does not apply to built-in tools like Task.

Exits 0 on any failure so Claude Code keeps running.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final
from typing import TextIO

from loguru import logger

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_subagent_proxy.hooks.destroy_detached import DestroyAgentDetachedCallable
from imbue.mngr_subagent_proxy.hooks.destroy_detached import destroy_agent_detached
from imbue.mngr_subagent_proxy.hooks.mngr_api import list_agents_by_name

ListAgentsByNameCallable = Callable[[], dict[str, AgentDetails] | None]

# Lifecycle states that mean "child is still doing real work" -- in
# those states, PostToolUse must NOT destroy the child or we throw away
# the user's work. Anything else (STOPPED, REPLACED, DONE,
# RUNNING_UNKNOWN_AGENT_TYPE, missing-from-mngr-list) is treated as
# "safe to destroy."
_LIVE_LIFECYCLE_STATES: Final[frozenset[AgentLifecycleState]] = frozenset(
    {AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING}
)


def _read_stdin_json(stdin: TextIO) -> dict[str, Any] | None:
    try:
        raw = stdin.read()
    except OSError as e:
        logger.warning("rewrite: failed to read stdin: {}", e)
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("rewrite: malformed stdin JSON: {}", e)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("rewrite: failed to remove {}: {}", path, e)


def _is_child_still_alive(
    target_name: str,
    list_callable: ListAgentsByNameCallable,
) -> bool:
    """Return True if the named child is currently in a live lifecycle state.

    On any failure (mngr list errors, target missing, unexpected state shape),
    return False so the caller falls back to a safe-to-destroy path. Caller
    combines this with the result_file existence check; either signal of
    "still doing work" wins.
    """
    agents = list_callable()
    if agents is None:
        # mngr list failed; fall back to result_file-only signal.
        return False
    agent = agents.get(target_name)
    if agent is None:
        # Already gone from the registry; nothing to preserve.
        return False
    return agent.state in _LIVE_LIFECYCLE_STATES


def run(
    stdin: TextIO,
    stdout: TextIO,
    destroy_callable: DestroyAgentDetachedCallable = destroy_agent_detached,
    list_callable: ListAgentsByNameCallable = list_agents_by_name,
) -> None:
    """PostToolUse:Agent hook core.

    Takes I/O streams and the detached-destroy callable explicitly so tests
    can inject StringIO buffers and a stub destroy function without
    monkey-patching module-level names.
    """
    os.umask(0o077)

    payload = _read_stdin_json(stdin)
    if payload is None:
        return

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir_env:
        return
    state_dir = Path(state_dir_env)

    tid = payload.get("tool_use_id")
    if not isinstance(tid, str) or not tid:
        return

    map_file = state_dir / "subagent_map" / f"{tid}.json"
    if not map_file.is_file():
        # Native subagent ran (PreToolUse passed through); nothing to do.
        return

    target_name = ""
    run_in_background = False
    try:
        map_data = json.loads(map_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("rewrite: failed to read map file {}: {}", map_file, e)
        map_data = None
    if isinstance(map_data, dict):
        raw_target = map_data.get("target_name")
        if isinstance(raw_target, str):
            target_name = raw_target
        run_in_background = bool(map_data.get("run_in_background", False))

    result_file = state_dir / "subagent_results" / f"{tid}.txt"
    prompt_file = state_dir / "subagent_prompts" / f"{tid}.md"
    script_file = state_dir / "proxy_commands" / f"wait-{tid}.sh"
    env_file = state_dir / "proxy_commands" / f"env-{tid}.env"
    init_flag = state_dir / "proxy_commands" / f"initialized-{tid}"
    watermark_file = state_dir / "proxy_commands" / f"watermark-{tid}"

    # Destroy the actual subagent FIRST -- it's the heavy/slow piece and
    # we want it kicked off before we touch any of the local files. The
    # destroy is fire-and-forget so this returns instantly.
    #
    # Skip destroy entirely in background mode: the parent expects the
    # subagent to keep running so it can be polled via `mngr transcript
    # <name>` / `mngr connect <name>`. The subagent's own end_turn +
    # mngr's normal lifecycle handle teardown when it's actually done,
    # and our on_before_agent_destroy cascade catches it on parent
    # destroy.
    #
    # Skip destroy when result_file is absent OR the child is currently
    # in a live lifecycle state (RUNNING / WAITING). Either signal means
    # the child is still doing real work and destroying it on the
    # parent's PostToolUse would throw away that work and leave the
    # user with no recovery path.
    #
    # - result_file-absent: subagent_wait never observed an END_TURN
    #   (Haiku bailed early -- timeout, hallucinated permission dialog,
    #   retry cap, etc.).
    # - lifecycle-live: catches edge cases the result_file proxy can
    #   miss (e.g. result_file appeared but child is still WAITING for
    #   permission resolution; subagent_wait crashed mid-stream after
    #   writing result_file but before the child stopped).
    #
    # Either signal "alive" wins; both must say "done" to destroy. The
    # on_before_agent_destroy cascade still tears these preserved
    # children down when the parent is destroyed, and the SessionStart
    # reaper sweeps orphaned ones on next parent boot.
    haiku_observed_end_turn = result_file.is_file()
    child_lifecycle_alive = bool(target_name) and _is_child_still_alive(target_name, list_callable)
    should_destroy = (
        bool(target_name) and not run_in_background and haiku_observed_end_turn and not child_lifecycle_alive
    )
    if should_destroy:
        destroy_log = state_dir / "subagent_destroy.log"
        destroy_callable(target_name, destroy_log)

    # Clean up state-dir-internal artifacts. Leave the wait-script and
    # init_flag in place so a stray re-invocation by Haiku (which can
    # happen if Haiku ignores the MNGR_PROXY_END_OF_OUTPUT sentinel and
    # loops on its Bash tool) finds an idempotent script that emits just
    # the sentinel and exits 0, rather than a missing-file error that
    # keeps Haiku looping. The SessionStart reaper sweeps stale
    # wait-script + init_flag pairs on the next parent boot.
    #
    # In background mode also retain the map_file: the spawned subagent
    # is still running and the on_before_agent_destroy cascade reads
    # subagent_map/ to find children to tear down when the parent is
    # destroyed. Deleting it here would orphan the background child.
    # The same logic applies when we're keeping the child alive due to
    # missing result_file: retain the map_file (and all sidefiles) so
    # SessionStart reaper can pick it up later.
    # watermark_file is a sidefile owned by subagent_wait; the wait-script
    # deletes it on END_TURN, but a SIGKILL'd Python process or parent crash
    # leaves it behind. Cleaning it here matches the other proxy_commands/
    # sidefiles and prevents orphaned watermarks from accumulating.
    if should_destroy:
        paths_to_remove = [env_file, prompt_file, result_file, watermark_file, map_file]
        for path in paths_to_remove:
            _best_effort_unlink(path)
    del script_file, init_flag  # intentionally retained


def main() -> None:
    """PostToolUse:Agent hook entry point. Wires up the real stdin/stdout."""
    run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
