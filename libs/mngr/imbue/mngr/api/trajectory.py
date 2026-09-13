"""Build full ATIF trajectory documents for agents, resolving subagents.

The pure merge logic lives in :mod:`imbue.mngr.agents.trajectory_build`; this
module supplies its inputs from the real world: it reads an agent's
common-transcript stream through the events API (so it works for remote
hosts), enriches the root from the agent's discovery data, and recursively
embeds the trajectories of claude subagents that mngr ran as sibling proxy
agents.
"""

from collections.abc import Sequence
from typing import Final

from imbue.mngr.agents.trajectory_build import EmbeddedSubagent
from imbue.mngr.agents.trajectory_build import MNGR_SUBAGENT_KIND
from imbue.mngr.agents.trajectory_build import TrajectoryBuildResult
from imbue.mngr.agents.trajectory_build import TrajectoryEnrichment
from imbue.mngr.agents.trajectory_build import build_trajectory_from_records
from imbue.mngr.agents.trajectory_build import parse_stream_content
from imbue.mngr.api.events import read_common_transcript_content
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.api.find import find_one_agent_and_agents_by_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import TrajectoryBuildError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import DiscoveredAgent

# The labels the claude subagent proxy plugin attaches to the sibling agents it
# spawns for Task tool calls (mirrored from mngr_claude_subagent_proxy, which is
# a plugin and therefore not importable from core; its README documents the
# labels as a stable contract).
SUBAGENT_PROXY_PARENT_ID_LABEL: Final[str] = "mngr_claude_subagent_proxy_parent_id"
SUBAGENT_PROXY_TOOL_USE_ID_LABEL: Final[str] = "mngr_claude_subagent_proxy_tool_use_id"

# ATIF requires agent.version; no agent type records a CLI/plugin version in its
# data.json today, so the spec-sanctioned fallback applies to all of them. Also
# used for agent.name when discovery has no type for the agent.
_UNKNOWN: Final[str] = "unknown"


def build_trajectory_for_agent(address: AgentAddress, mngr_ctx: MngrContext) -> TrajectoryBuildResult:
    """Build the agent's stream into a validated ATIF document, embedding subagents.

    Raises :class:`TrajectoryBuildError` (or :class:`MngrError` for read
    failures) when no valid document can be produced; per-subagent problems
    degrade to warnings, leaving the delegating call's plain textual result
    untouched.
    """
    host_ref, agent_ref, agents_by_host = find_one_agent_and_agents_by_host(address, mngr_ctx)
    return _build_for_discovered_agent(
        agent_ref=agent_ref,
        same_host_agents=list(agents_by_host[host_ref]),
        mngr_ctx=mngr_ctx,
        visited_agent_ids=frozenset({str(agent_ref.agent_id)}),
    )


def _build_for_discovered_agent(
    agent_ref: DiscoveredAgent,
    same_host_agents: Sequence[DiscoveredAgent],
    mngr_ctx: MngrContext,
    # The ids of this agent and every ancestor it is being embedded under, so a cycle
    # in the parent labels cannot send the recursion around forever.
    visited_agent_ids: frozenset[str],
) -> TrajectoryBuildResult:
    # Read the agent's stream through the events API.
    target = try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=agent_ref.agent_id,
        agent_name=str(agent_ref.agent_name),
        host_id=agent_ref.host_id,
        provider_name=agent_ref.provider_name,
    )
    if target is None:
        raise TrajectoryBuildError(f"Cannot read events for agent '{agent_ref.agent_name}': host is not readable")
    _event_file_name, stream_content = read_common_transcript_content(target)
    records = parse_stream_content(stream_content, source_description=target.display_name)
    warnings: list[str] = []

    # Recursively build the trajectories of proxy-run subagents (siblings labeled
    # with this agent's id and the delegating Task call's tool_use_id). A subagent
    # that cannot be built (destroyed, unreadable, invalid stream) is skipped with
    # a warning, leaving its delegating call's textual result untouched.
    subagent_by_call_id: dict[str, EmbeddedSubagent] = {}
    for child_ref in same_host_agents:
        child_labels = child_ref.labels
        if child_labels.get(SUBAGENT_PROXY_PARENT_ID_LABEL) != str(agent_ref.agent_id):
            continue
        tool_use_id = child_labels.get(SUBAGENT_PROXY_TOOL_USE_ID_LABEL)
        if tool_use_id is None:
            continue
        child_agent_id = str(child_ref.agent_id)
        if child_agent_id in visited_agent_ids:
            warnings.append(
                f"Skipped subagent '{child_ref.agent_name}' for tool call '{tool_use_id}': it is already an "
                "ancestor of this trajectory (the parent labels form a cycle)"
            )
            continue
        try:
            child_result = _build_for_discovered_agent(
                agent_ref=child_ref,
                same_host_agents=same_host_agents,
                mngr_ctx=mngr_ctx,
                visited_agent_ids=visited_agent_ids | {child_agent_id},
            )
        except MngrError as e:
            warnings.append(f"Skipped subagent '{child_ref.agent_name}' for tool call '{tool_use_id}': {e}")
            continue
        warnings.extend(child_result.warnings)
        subagent_by_call_id[tool_use_id] = EmbeddedSubagent(
            trajectory=child_result.trajectory,
            subagent_kind=MNGR_SUBAGENT_KIND,
        )

    enrichment = TrajectoryEnrichment(
        agent_name=str(agent_ref.agent_type) if agent_ref.agent_type is not None else _UNKNOWN,
        agent_version=_UNKNOWN,
        session_id=str(agent_ref.agent_id),
        trajectory_id=str(agent_ref.agent_id),
    )
    build_result = build_trajectory_from_records(
        records=records,
        enrichment=enrichment,
        subagent_by_call_id=subagent_by_call_id,
    )
    return TrajectoryBuildResult(
        trajectory=build_result.trajectory,
        warnings=tuple(warnings) + build_result.warnings,
    )
