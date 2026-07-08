"""In-process helpers around ``imbue.mngr.api`` for subagent-proxy hooks.

All helpers are best-effort: they log and swallow errors so a hook
invocation never crashes on transient mngr failures.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable
from typing import Iterator

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.cleanup import execute_cleanup
from imbue.mngr.api.cleanup import find_agents_for_cleanup
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.main import get_or_create_plugin_manager
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CleanupAction
from imbue.mngr.primitives import ErrorBehavior

# DI signature for ``list_agents_by_name``. Lives with the function so every
# caller (hooks/cleanup.py, hooks/reap.py) imports the same alias and tests
# have a single name to inject against. Mirrors the pattern of
# ``DestroyAgentDetachedCallable`` in ``hooks/destroy_detached.py``.
ListAgentsByNameCallable = Callable[[], dict[str, AgentDetails] | None]

# Label set by both PROXY and DENY-mode spawning paths on every child agent:
# PROXY's wait-script attaches it via ``mngr create --label ...`` (see
# hooks/spawn.py), and the ``mngr-proxy`` skill instructs Claude to
# attach it the same way for DENY-mode-spawned children. Used as the
# server-side source of truth for "is this agent one of my children".
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
    work the user wants to observe or capture); only terminal children
    (DONE / STOPPED) are reaped. Same scope and semantics across PROXY
    and DENY modes.
    """
    children: list[AgentDetails] = []
    for agent in agents_by_name.values():
        if agent.labels.get(PARENT_ID_LABEL) != parent_id:
            continue
        if agent.state not in _TERMINAL_STATES:
            continue
        children.append(agent)
    return children


@contextmanager
def build_mngr_ctx(group_name: str) -> Iterator[MngrContext]:
    """Build a short-lived MngrContext + ConcurrencyGroup for a single call."""
    pm = get_or_create_plugin_manager()
    with ConcurrencyGroup(name=group_name) as cg:
        ctx = load_config(pm, cg, is_interactive=False)
        yield ctx


def list_agents_by_name() -> dict[str, AgentDetails] | None:
    """In-process list of agents keyed by name. Returns None on failure."""
    try:
        with build_mngr_ctx("subagent-proxy-list") as ctx:
            result = list_agents(mngr_ctx=ctx, is_streaming=False, error_behavior=ErrorBehavior.CONTINUE)
    except MngrError as e:
        logger.warning("list_agents_by_name: mngr list failed: {}", e)
        return None
    return {agent.name: agent for agent in result.agents}


def destroy_agent_sync(target_name: str) -> None:
    """In-process destroy of a single mngr agent by name. Best-effort."""
    try:
        with build_mngr_ctx("subagent-proxy-destroy") as ctx:
            filter_expr = f'name == "{target_name}"'
            agents = find_agents_for_cleanup(
                mngr_ctx=ctx,
                include_filters=(filter_expr,),
                exclude_filters=(),
                error_behavior=ErrorBehavior.CONTINUE,
            )
            if not agents:
                logger.debug("destroy_agent_sync: no agent named {} found", target_name)
                return
            execute_cleanup(
                mngr_ctx=ctx,
                agents=agents,
                action=CleanupAction.DESTROY,
                is_dry_run=False,
                error_behavior=ErrorBehavior.CONTINUE,
            )
    except MngrError as e:
        logger.warning("destroy_agent_sync: destroy of {} failed: {}", target_name, e)
