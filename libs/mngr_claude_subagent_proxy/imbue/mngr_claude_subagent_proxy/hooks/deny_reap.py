"""SessionStart hook for DENY mode. Reaps terminal subagents the parent
spawned in previous sessions, identified by the
``mngr_claude_subagent_proxy_parent_id`` label.

DENY mode never writes ``subagent_map/`` sidefiles (that's PROXY mode's
mechanism), so the PROXY reaper at ``hooks/reap.py`` doesn't apply.
Instead, the skill instructs Claude to label every spawned subagent
with this parent's ``MNGR_AGENT_ID``; this hook queries by that label
and destroys any terminal children left behind by a previous session.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from loguru import logger

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_claude_subagent_proxy.hooks.destroy_detached import DestroyAgentDetachedCallable
from imbue.mngr_claude_subagent_proxy.hooks.destroy_detached import destroy_agent_detached
from imbue.mngr_claude_subagent_proxy.hooks.mngr_api import ListAgentsByNameCallable
from imbue.mngr_claude_subagent_proxy.hooks.mngr_api import list_agents_by_name

PARENT_ID_LABEL: str = "mngr_claude_subagent_proxy_parent_id"
_TERMINAL_STATES: frozenset[AgentLifecycleState] = frozenset({AgentLifecycleState.DONE, AgentLifecycleState.STOPPED})


def find_terminal_children(
    parent_id: str,
    agents_by_name: dict[str, AgentDetails],
) -> list[AgentDetails]:
    """Filter the agent list to terminal children of ``parent_id``.

    A child is identified by the ``mngr_claude_subagent_proxy_parent_id``
    label matching this parent's ``MNGR_AGENT_ID``. RUNNING / WAITING
    children are deliberately left alone (they may still be doing useful
    work that the user wants to observe or capture); only terminal
    children (DONE / STOPPED) are reaped, matching PROXY mode's
    conservative cleanup scope.
    """
    children: list[AgentDetails] = []
    for agent in agents_by_name.values():
        if agent.labels.get(PARENT_ID_LABEL) != parent_id:
            continue
        if agent.state not in _TERMINAL_STATES:
            continue
        children.append(agent)
    return children


def run(
    stdin: TextIO,
    list_callable: ListAgentsByNameCallable = list_agents_by_name,
    destroy_callable: DestroyAgentDetachedCallable = destroy_agent_detached,
) -> None:
    """SessionStart hook core for DENY mode.

    All side-effecting dependencies are accepted as keyword arguments
    with production defaults so tests can pass stubs without
    monkey-patching module-level names.
    """
    try:
        stdin.read()
    except OSError:
        pass

    parent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not parent_id:
        logger.warning("deny_reap: MNGR_AGENT_ID unset; skipping label-based reap")
        return

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir_env:
        logger.warning("deny_reap: MNGR_AGENT_STATE_DIR unset; skipping label-based reap")
        return

    agents_by_name = list_callable()
    if agents_by_name is None:
        return

    terminals = find_terminal_children(parent_id, agents_by_name)
    if not terminals:
        return

    destroy_log = Path(state_dir_env) / "subagent_destroy.log"

    logger.info(
        "deny_reap: parent {} has {} terminal child(ren) to reap (label {}={})",
        parent_id,
        len(terminals),
        PARENT_ID_LABEL,
        parent_id,
    )
    for child in terminals:
        destroy_callable(child.name, destroy_log)


def main() -> None:
    """SessionStart hook entry point. Wires up the real stdin and helpers."""
    run(sys.stdin)


if __name__ == "__main__":
    main()
