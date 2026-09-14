"""Build full ATIF trajectory documents for agents, resolving subagents.

The pure merge logic lives in :mod:`imbue.mngr.agents.trajectory_build`; this
module supplies its inputs from the real world: it reads an agent's
common-transcript stream through the events API (so it works for remote
hosts), enriches the root from the agent's discovery data, and recursively
embeds the trajectories of claude subagents that mngr ran as sibling proxy
agents.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.agents.trajectory_build import EmbeddedSubagent
from imbue.mngr.agents.trajectory_build import MNGR_SUBAGENT_KIND
from imbue.mngr.agents.trajectory_build import TrajectoryBuildResult
from imbue.mngr.agents.trajectory_build import TrajectoryEnrichment
from imbue.mngr.agents.trajectory_build import build_trajectory_from_records
from imbue.mngr.agents.trajectory_build import parse_stream_content
from imbue.mngr.api.events import read_common_transcript_content
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.api.find import find_one_agent_and_agents_by_host
from imbue.mngr.api.preservation import PreservationManifest
from imbue.mngr.api.preservation import PreservationOutcome
from imbue.mngr.api.preservation import PreservedAgentArchive
from imbue.mngr.api.preservation import PreservedItemResult
from imbue.mngr.api.preservation import discover_preserved_agent_archives
from imbue.mngr.api.preservation import preserved_agent_matches
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import TrajectoryBuildError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName

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


def build_trajectory_for_agent(
    address: AgentAddress, mngr_ctx: MngrContext, *, include_preserved: bool = False
) -> TrajectoryBuildResult:
    """Build the agent's stream into a validated ATIF document, embedding subagents.

    Only live agents are embedded unless ``include_preserved`` is set, which also walks the
    caller-local preservation archives so a subagent destroyed after finishing still appears
    under the call that delegated to it.

    Raises :class:`TrajectoryBuildError` (or :class:`MngrError` for read
    failures) when no valid document can be produced; per-subagent problems
    degrade to warnings, leaving the delegating call's plain textual result
    untouched.
    """
    host_ref, agent_ref, agents_by_host = find_one_agent_and_agents_by_host(address, mngr_ctx)
    warnings: tuple[str, ...] = ()
    try:
        archives = (
            discover_preserved_agent_archives(Path(mngr_ctx.config.default_host_dir).expanduser())
            if include_preserved
            else []
        )
    except MngrError as exc:
        archives = []
        warnings = (f"Could not discover preserved subagents: {exc}",)
    result = _build_for_discovered_agent(
        agent_ref=agent_ref,
        same_host_agents=list(agents_by_host[host_ref]),
        preserved_archives=archives,
        mngr_ctx=mngr_ctx,
        visited_agent_ids=frozenset({agent_ref.agent_id}),
    )
    return result.prepend_warnings(warnings)


def build_trajectory_for_preserved_agent(
    archive: PreservedAgentArchive, archives: Sequence[PreservedAgentArchive]
) -> TrajectoryBuildResult:
    """Build an ATIF document from an already-resolved caller-local preserved agent archive.

    ``archives`` is the discovery listing ``archive`` was resolved from, and is where the
    preserved proxy children embedded under it are looked for; a listing that does not hold
    them leaves their delegating calls with their plain textual results. Callers resolve the
    archive with :func:`find_one_preserved_archive` over that same listing.
    """
    return _build_for_preserved_archive(
        archive=archive,
        archives=archives,
        visited_agent_ids=frozenset({archive.agent_id}),
    )


def _build_for_discovered_agent(
    agent_ref: DiscoveredAgent,
    same_host_agents: Sequence[DiscoveredAgent],
    preserved_archives: Sequence[PreservedAgentArchive],
    mngr_ctx: MngrContext,
    # The ids of this agent and every ancestor it is being embedded under, so a cycle
    # in the parent labels cannot send the recursion around forever.
    visited_agent_ids: frozenset[AgentId],
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

    subagent_by_call_id, live_warnings = _embed_live_children(
        agent_ref=agent_ref,
        same_host_agents=same_host_agents,
        preserved_archives=preserved_archives,
        mngr_ctx=mngr_ctx,
        visited_agent_ids=visited_agent_ids,
    )
    warnings.extend(live_warnings)

    # Destroyed proxy children disappear from normal discovery, but their
    # preservation manifests retain the parent and tool-call labels.
    preserved_subagents, preserved_warnings = _embed_preserved_children(
        parent_agent_id=agent_ref.agent_id,
        parent_host_id=agent_ref.host_id,
        parent_provider_name=agent_ref.provider_name,
        archives=preserved_archives,
        visited_agent_ids=visited_agent_ids,
        filled_tool_use_ids=frozenset(subagent_by_call_id),
    )
    warnings.extend(preserved_warnings)
    subagent_by_call_id.update(preserved_subagents)

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
    return build_result.prepend_warnings(warnings)


def _embed_live_children(
    *,
    agent_ref: DiscoveredAgent,
    same_host_agents: Sequence[DiscoveredAgent],
    preserved_archives: Sequence[PreservedAgentArchive],
    mngr_ctx: MngrContext,
    visited_agent_ids: frozenset[AgentId],
) -> tuple[dict[str, EmbeddedSubagent], list[str]]:
    """Build the still-discoverable proxy children of a parent, by delegating tool call.

    A child is a sibling on the same host labeled with this parent's id and the delegating
    Task call's tool_use_id. One that cannot be built (unreadable, invalid stream, an
    ancestor of this trajectory) is left unembedded with a warning, so the delegating call
    keeps its plain textual result.
    """
    subagent_by_call_id: dict[str, EmbeddedSubagent] = {}
    warnings: list[str] = []
    for child_ref in same_host_agents:
        child_labels = child_ref.labels
        if child_labels.get(SUBAGENT_PROXY_PARENT_ID_LABEL) != agent_ref.agent_id:
            continue
        tool_use_id = child_labels.get(SUBAGENT_PROXY_TOOL_USE_ID_LABEL)
        if tool_use_id is None:
            continue
        child_agent_id = child_ref.agent_id
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
                preserved_archives=preserved_archives,
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
    return subagent_by_call_id, warnings


@pure
def find_one_preserved_archive(
    address: AgentAddress, archives: Sequence[PreservedAgentArchive]
) -> PreservedAgentArchive:
    """Resolve a preserved archive with the same unambiguous name-or-id rule as discovery.

    Where several archives share the requested name, an archive whose manifest establishes that
    no common transcript was copied is not one the request can be answered from, so it does not
    make the request ambiguous -- the same narrowing :func:`_viable_preserved_children` applies
    to the archives claiming a tool call. It narrows only a tie: a single match is returned
    whether or not it can be read, so its own read failure is what the caller is told about.
    """
    name_matches = [archive for archive in archives if preserved_agent_matches(archive, address.agent)]
    matches = name_matches
    if address.host is not None:
        # An archive that recorded no origin host cannot be shown to have come from the
        # requested one, so a qualifier excludes it rather than trusting the name alone.
        matches = [
            archive
            for archive in name_matches
            if archive.identity is not None
            and address.host.matches_host(
                archive.identity.host_id,
                archive.identity.host_name,
                archive.identity.provider_name,
            )
        ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise TrajectoryBuildError(
            f"No preserved agent archive matches '{address}'{_host_qualifier_exclusion_detail(name_matches)}"
        )
    readable = [archive for archive in matches if _unreadable_transcript_detail(archive) is None]
    if len(readable) == 1:
        return readable[0]
    locations = ", ".join(str(archive.path) for archive in matches)
    raise TrajectoryBuildError(
        f"Multiple preserved agent archives match '{address}': {locations}. {_narrowing_advice(matches)}"
    )


@pure
def _host_qualifier_exclusion_detail(excluded: Sequence[PreservedAgentArchive]) -> str:
    """Say which name-matching archives a host qualifier excluded, and what each recorded.

    Empty when the qualifier excluded nothing, so the caller can append it unconditionally.
    """
    if not excluded:
        return ""
    descriptions: list[str] = []
    for archive in excluded:
        identity = archive.identity
        origin = (
            f"ran on {identity.host_name}.{identity.provider_name}"
            if identity is not None
            else "recorded no origin host, so no qualifier can match it"
        )
        descriptions.append(f"{archive.path} ({origin})")
    return (
        ": the host qualifier excluded " + ", ".join(descriptions) + ". Drop the host qualifier, or "
        "select the archive by agent id."
    )


@pure
def _narrowing_advice(matches: Sequence[PreservedAgentArchive]) -> str:
    """How to pick one of several archives that share the requested agent name.

    A host qualifier excludes every archive that recorded no origin host, so where any of the
    matches is one of those, suggesting a qualifier would turn the ambiguity into a no-match.
    """
    if all(archive.identity is not None for archive in matches):
        return "Select an unambiguous agent name or id, adding a host qualifier where needed."
    return (
        "Select the agent id: some of these archives recorded no origin host, so a host qualifier "
        "would exclude them rather than choose between them."
    )


@pure
def _common_transcript_items(manifest: PreservationManifest) -> list[PreservedItemResult]:
    """The manifest's results for the common transcript directories a preservation pass requested.

    The ``events/<source>/common_transcript`` layout these paths follow is owned by
    :func:`imbue.mngr.api.preservation.build_transcript_preserved_items`, which declares the
    items; keep the two in step.
    """
    return [
        item
        for item in manifest.items
        if item.rel_path.startswith("events/") and item.rel_path.endswith("/common_transcript")
    ]


@pure
def _format_item_outcomes(items: Sequence[PreservedItemResult]) -> str:
    """Render what each copy attempt observed, naming the error where one was recorded."""
    return ", ".join(f"{item.rel_path}={item.outcome}" + (f" ({item.error})" if item.error else "") for item in items)


def read_preserved_common_transcript(archive: PreservedAgentArchive) -> tuple[Path, str]:
    """Read the single common transcript copied into a preserved archive."""
    candidates = sorted(archive.path.glob("events/*/common_transcript/events.jsonl"))
    if archive.manifest is not None:
        copied_dirs = {
            archive.path / item.rel_path
            for item in _common_transcript_items(archive.manifest)
            if item.outcome == PreservationOutcome.COPIED
        }
        candidates = [path for path in candidates if path.parent in copied_dirs]
    if not candidates:
        item_details = ""
        if archive.manifest is not None:
            transcript_items = _common_transcript_items(archive.manifest)
            if transcript_items:
                item_details = "; preservation result: " + _format_item_outcomes(transcript_items)
        raw_paths = sorted(archive.path.glob("logs/*_transcript"))
        raw_detail = (
            "; native raw transcript is available at " + ", ".join(str(path) for path in raw_paths)
            if raw_paths
            else "; no native raw transcript directory was found"
        )
        raise TrajectoryBuildError(
            f"Preserved archive '{archive.path}' for '{archive.agent_name}' has no copied common transcript"
            f"{item_details}{raw_detail}"
        )
    if len(candidates) > 1:
        raise TrajectoryBuildError(
            f"Preserved archive for '{archive.agent_name}' has multiple common transcripts: "
            + ", ".join(str(path) for path in candidates)
        )
    path = candidates[0]
    try:
        return path, path.read_text()
    except OSError as e:
        raise TrajectoryBuildError(f"Could not read preserved transcript '{path}': {e}") from e


@pure
def _preserved_children_by_tool_call(
    *,
    parent_agent_id: AgentId,
    parent_host_id: HostId,
    parent_provider_name: ProviderInstanceName,
    archives: Sequence[PreservedAgentArchive],
) -> dict[str, list[PreservedAgentArchive]]:
    """Group the proxy children that recorded this parent's origin host, by delegating tool call.

    An archive that recorded no provenance carries no labels to group by, whether or not it
    has a manifest, so it is never a child here.
    """
    children: dict[str, list[PreservedAgentArchive]] = {}
    for archive in archives:
        identity = archive.identity
        if (
            identity is None
            or identity.host_id != parent_host_id
            or identity.provider_name != parent_provider_name
            or identity.labels.get(SUBAGENT_PROXY_PARENT_ID_LABEL) != parent_agent_id
        ):
            continue
        tool_use_id = identity.labels.get(SUBAGENT_PROXY_TOOL_USE_ID_LABEL)
        if tool_use_id is not None:
            children.setdefault(tool_use_id, []).append(archive)
    return children


@pure
def _ambiguous_preserved_children_warning(tool_use_id: str, candidates: Sequence[PreservedAgentArchive]) -> str:
    paths = ", ".join(str(candidate.path) for candidate in candidates)
    return f"Skipped all preserved subagents for tool call '{tool_use_id}': multiple archives claim it ({paths})"


@pure
def _unreadable_transcript_detail(archive: PreservedAgentArchive) -> str | None:
    """Why the archive's manifest rules out reading a common transcript, or None if it does not.

    An archive with no manifest records nothing either way, so nothing rules it out here and
    only an attempt to read its files can tell.
    """
    manifest = archive.manifest
    if manifest is None:
        return None
    transcript_items = _common_transcript_items(manifest)
    if any(item.outcome == PreservationOutcome.COPIED for item in transcript_items):
        return None
    return _format_item_outcomes(transcript_items) or "manifest records no common transcript item"


@pure
def _viable_preserved_children(
    tool_use_id: str,
    candidates: Sequence[PreservedAgentArchive],
) -> tuple[list[PreservedAgentArchive], list[str]]:
    """Exclude archives whose manifest establishes that no common transcript was copied."""
    viable: list[PreservedAgentArchive] = []
    warnings: list[str] = []
    for candidate in candidates:
        detail = _unreadable_transcript_detail(candidate)
        if detail is None:
            viable.append(candidate)
            continue
        warnings.append(f"Skipped preserved subagent '{candidate.agent_name}' for tool call '{tool_use_id}': {detail}")
    return viable, warnings


def _embed_preserved_children(
    *,
    parent_agent_id: AgentId,
    parent_host_id: HostId,
    parent_provider_name: ProviderInstanceName,
    archives: Sequence[PreservedAgentArchive],
    visited_agent_ids: frozenset[AgentId],
    # Tool calls a live child already answers; an archive never displaces one.
    filled_tool_use_ids: frozenset[str],
) -> tuple[dict[str, EmbeddedSubagent], list[str]]:
    """Build the preserved archives that a parent's proxy children left behind, by tool call.

    A call whose archive cannot be chosen (several claim it) or cannot be built
    (no copied transcript, unreadable, invalid stream, an ancestor of this
    trajectory) is left unembedded with a warning, so the delegating call keeps
    its plain textual result.
    """
    subagent_by_call_id: dict[str, EmbeddedSubagent] = {}
    warnings: list[str] = []
    preserved_by_call = _preserved_children_by_tool_call(
        parent_agent_id=parent_agent_id,
        parent_host_id=parent_host_id,
        parent_provider_name=parent_provider_name,
        archives=archives,
    )
    for tool_use_id, claimants in preserved_by_call.items():
        if tool_use_id in filled_tool_use_ids:
            continue
        candidates, candidate_warnings = _viable_preserved_children(tool_use_id, claimants)
        warnings.extend(candidate_warnings)
        if not candidates:
            continue
        if len(candidates) > 1:
            warnings.append(_ambiguous_preserved_children_warning(tool_use_id, candidates))
            continue
        child = candidates[0]
        child_id = child.agent_id
        if child_id in visited_agent_ids:
            warnings.append(
                f"Skipped preserved subagent '{child.agent_name}' for tool call '{tool_use_id}': "
                "it is already an ancestor of this trajectory"
            )
            continue
        try:
            child_result = _build_for_preserved_archive(
                archive=child,
                archives=archives,
                visited_agent_ids=visited_agent_ids | {child_id},
            )
        except MngrError as e:
            warnings.append(f"Skipped preserved subagent '{child.agent_name}' for tool call '{tool_use_id}': {e}")
            continue
        warnings.extend(child_result.warnings)
        subagent_by_call_id[tool_use_id] = EmbeddedSubagent(
            trajectory=child_result.trajectory,
            subagent_kind=MNGR_SUBAGENT_KIND,
        )
    return subagent_by_call_id, warnings


def _build_for_preserved_archive(
    archive: PreservedAgentArchive,
    archives: Sequence[PreservedAgentArchive],
    visited_agent_ids: frozenset[AgentId],
) -> TrajectoryBuildResult:
    """Build one preserved archive and recursively embed preserved proxy children.

    Only preserved children are embedded: a proxy child that is still live when its parent
    was destroyed is left out, and its delegating call keeps its plain textual result. There
    is no discovery here to find such a child with -- the archive names the host it ran on,
    not a host that is necessarily still reachable.
    """
    stream_path, stream_content = read_preserved_common_transcript(archive)
    records = parse_stream_content(stream_content, source_description=f"preserved transcript '{stream_path}'")
    warnings: list[str] = []
    subagent_by_call_id: dict[str, EmbeddedSubagent] = {}
    # An archive that recorded no provenance has no labels, so it can name neither its
    # origin host nor the children delegated to it.
    identity = archive.identity
    if identity is not None:
        subagent_by_call_id, warnings = _embed_preserved_children(
            parent_agent_id=archive.agent_id,
            parent_host_id=identity.host_id,
            parent_provider_name=identity.provider_name,
            archives=archives,
            visited_agent_ids=visited_agent_ids,
            filled_tool_use_ids=frozenset(),
        )

    build_result = build_trajectory_from_records(
        records=records,
        enrichment=TrajectoryEnrichment(
            agent_name=str(identity.agent_type) if identity is not None else _UNKNOWN,
            agent_version=_UNKNOWN,
            session_id=str(archive.agent_id),
            trajectory_id=str(archive.agent_id),
        ),
        subagent_by_call_id=subagent_by_call_id,
    )
    return build_result.prepend_warnings(warnings)
