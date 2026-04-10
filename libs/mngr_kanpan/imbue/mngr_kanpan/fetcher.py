import time
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_source import deserialize_fields
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import DataSourceConfig
from imbue.mngr_kanpan.data_types import KanpanPluginConfig

PLUGIN_NAME = "kanpan"


def fetch_board_snapshot(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> BoardSnapshot:
    """Full fetch: list agents, run data sources in parallel, build board entries.

    1. List agents
    2. Load cached fields per source (only enabled sources)
    3. Merge cached fields into flat dict
    4. Run ALL data sources in parallel
    5. Persist per-source fields
    6. Compute board sections from typed fields
    7. Build AgentBoardEntry with pre-computed CellDisplay
    """
    start_time = time.monotonic()
    errors: list[str] = []

    result = list_agents(
        mngr_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.CONTINUE,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )
    for error in result.errors:
        errors.append(f"{error.exception_type}: {error.message}")

    agents = tuple(result.agents)

    # Load muted state from certified data
    muted_agents = _load_muted_agents(mngr_ctx)

    # Load cached fields per source (only for enabled sources)
    cached_by_source = _load_cached_fields(mngr_ctx, data_sources, agents)

    # Merge all cached fields into flat dict: dict[AgentName, dict[str, FieldValue]]
    merged_cached = _merge_cached_fields(cached_by_source)

    # Run all data sources in parallel
    new_fields_by_source, source_errors = _run_data_sources_parallel(data_sources, agents, merged_cached, mngr_ctx)
    errors.extend(source_errors)

    # Persist per-source fields
    _persist_fields(mngr_ctx, new_fields_by_source, agents)

    # Merge new fields into final flat dict
    all_fields: dict[AgentName, dict[str, FieldValue]] = {}
    for _source_name, source_fields in new_fields_by_source.items():
        for agent_name, agent_fields in source_fields.items():
            if agent_name not in all_fields:
                all_fields[agent_name] = {}
            all_fields[agent_name].update(agent_fields)

    # Build board entries
    entries: list[AgentBoardEntry] = []
    for agent in agents:
        agent_fields = dict(all_fields.get(agent.name, {}))
        is_muted = agent.name in muted_agents
        agent_fields[FIELD_MUTED] = BoolField(value=is_muted)

        # Compute cell displays from fields
        cells = {key: field.display() for key, field in agent_fields.items()}

        section = compute_section(agent_fields)
        entries.append(
            AgentBoardEntry(
                name=agent.name,
                state=agent.state,
                provider_name=agent.host.provider_name,
                branch=agent.initial_branch,
                is_muted=is_muted,
                fields=agent_fields,
                cells=cells,
                section=section,
            )
        )

    elapsed = time.monotonic() - start_time
    return BoardSnapshot(
        entries=tuple(entries),
        errors=tuple(errors),
        fetch_time_seconds=elapsed,
    )


def fetch_local_snapshot(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> BoardSnapshot:
    """Local-only snapshot: runs only non-remote data sources (repo_paths, git_info).

    Skips GitHub and shell data sources for speed.
    """
    local_sources = [s for s in data_sources if s.name in ("repo_paths", "git_info")]
    return fetch_board_snapshot(
        mngr_ctx,
        local_sources,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )


def _load_cached_fields(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
    agents: tuple[AgentDetails, ...],
) -> dict[str, dict[AgentName, dict[str, FieldValue]]]:
    """Load per-source cached fields from agent plugin data.

    Only loads for currently enabled data sources. Returns dict[source_name -> agent_name -> fields].
    """
    cached_by_source: dict[str, dict[AgentName, dict[str, FieldValue]]] = {}

    # Build type map for all sources
    source_field_types: dict[str, dict[str, type[FieldValue]]] = {}
    for source in data_sources:
        source_field_types[source.name] = source.field_types

    # Load cached data from agent plugin data
    try:
        agents_by_host, _ = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=None,
            agent_identifiers=None,
            include_destroyed=False,
            reset_caches=False,
        )
        for _host_ref, agent_refs in agents_by_host.items():
            for agent_ref in agent_refs:
                agent_name = agent_ref.agent_name
                kanpan_data = agent_ref.certified_data.get("plugin", {}).get(PLUGIN_NAME, {})
                sources_data = kanpan_data.get("data_sources", {})

                for source_name, field_types in source_field_types.items():
                    source_data = sources_data.get(source_name, {})
                    if source_data:
                        deserialized = deserialize_fields(source_data, field_types)
                        if deserialized:
                            if source_name not in cached_by_source:
                                cached_by_source[source_name] = {}
                            cached_by_source[source_name][agent_name] = deserialized
    except Exception as e:
        logger.debug("Failed to load cached fields: {}", e)

    return cached_by_source


def _merge_cached_fields(
    cached_by_source: dict[str, dict[AgentName, dict[str, FieldValue]]],
) -> dict[AgentName, dict[str, FieldValue]]:
    """Merge per-source cached fields into a single flat dict per agent."""
    merged: dict[AgentName, dict[str, FieldValue]] = {}
    for _source_name, source_fields in cached_by_source.items():
        for agent_name, agent_fields in source_fields.items():
            if agent_name not in merged:
                merged[agent_name] = {}
            merged[agent_name].update(agent_fields)
    return merged


def _run_data_sources_parallel(
    data_sources: Sequence[KanpanDataSource],
    agents: tuple[AgentDetails, ...],
    cached_fields: dict[AgentName, dict[str, FieldValue]],
    mngr_ctx: MngrContext,
) -> tuple[dict[str, dict[AgentName, dict[str, FieldValue]]], list[str]]:
    """Run all data sources in parallel. Returns (results_by_source_name, errors)."""
    all_errors: list[str] = []
    results: dict[str, dict[AgentName, dict[str, FieldValue]]] = {}

    if not data_sources:
        return results, all_errors

    with ThreadPoolExecutor(max_workers=min(len(data_sources), 8)) as executor:
        futures: dict[str, Future[tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]]] = {}
        for source in data_sources:
            futures[source.name] = executor.submit(source.compute, agents, cached_fields, mngr_ctx)

        for source_name, future in futures.items():
            try:
                source_fields, source_errors = future.result()
                results[source_name] = source_fields
                all_errors.extend(source_errors)
            except Exception as e:
                all_errors.append(f"Data source '{source_name}' failed: {e}")
                logger.debug("Data source '{}' failed: {}", source_name, e)

    return results, all_errors


def _persist_fields(
    mngr_ctx: MngrContext,
    fields_by_source: dict[str, dict[AgentName, dict[str, FieldValue]]],
    agents: tuple[AgentDetails, ...],
) -> None:
    """Persist per-source fields to agent plugin data."""
    try:
        agents_by_host, _ = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=None,
            agent_identifiers=None,
            include_destroyed=False,
            reset_caches=False,
        )

        # Build agent interface lookup
        agent_interfaces: dict[AgentName, Any] = {}
        for _host_ref, agent_refs in agents_by_host.items():
            for agent_ref in agent_refs:
                agent_interfaces[agent_ref.agent_name] = agent_ref

        for agent in agents:
            agent_ref = agent_interfaces.get(agent.name)
            if agent_ref is None:
                continue

            plugin_data = agent_ref.certified_data.get("plugin", {}).get(PLUGIN_NAME, {})
            sources_data = dict(plugin_data.get("data_sources", {}))

            # Update with new fields from each source
            for source_name, source_fields in fields_by_source.items():
                agent_fields = source_fields.get(agent.name, {})
                if agent_fields:
                    sources_data[source_name] = {key: field.model_dump() for key, field in agent_fields.items()}
                elif source_name in sources_data:
                    # Source ran but produced no fields for this agent -- clear stale data
                    del sources_data[source_name]

            # Write back
            new_plugin_data = dict(plugin_data)
            new_plugin_data["data_sources"] = sources_data
            agent_ref.set_plugin_data(PLUGIN_NAME, new_plugin_data)

    except Exception as e:
        logger.debug("Failed to persist fields: {}", e)


@pure
def compute_section(fields: dict[str, FieldValue]) -> BoardSection:
    """Compute the board section for an agent based on its typed fields."""
    muted = fields.get(FIELD_MUTED)
    if muted is not None:
        if not isinstance(muted, BoolField):
            raise KanpanFieldTypeError(f"Expected BoolField for 'muted', got {type(muted).__name__}")
        if muted.value:
            return BoardSection.MUTED

    pr = fields.get(FIELD_PR)
    if pr is None:
        return BoardSection.STILL_COOKING
    if not isinstance(pr, PrField):
        raise KanpanFieldTypeError(f"Expected PrField for 'pr', got {type(pr).__name__}")

    if pr.is_draft:
        return BoardSection.STILL_COOKING
    match pr.state:
        case PrState.MERGED:
            return BoardSection.PR_MERGED
        case PrState.CLOSED:
            return BoardSection.PR_CLOSED
        case PrState.OPEN:
            ci = fields.get(FIELD_CI)
            match ci:
                case None:
                    return BoardSection.PR_BEING_REVIEWED
                case CiField():
                    pass
                case _:
                    raise KanpanFieldTypeError(f"Expected CiField for 'ci', got {type(ci).__name__}")
            match ci.status:
                case CiStatus.FAILING:
                    return BoardSection.PRS_FAILED
                case CiStatus.PASSING | CiStatus.PENDING | CiStatus.UNKNOWN:
                    return BoardSection.PR_BEING_REVIEWED


def toggle_agent_mute(mngr_ctx: MngrContext, agent_name: AgentName) -> bool:
    """Toggle the mute state of an agent. Returns the new mute state."""
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=(str(agent_name),),
        include_destroyed=False,
        reset_caches=False,
    )
    agent, _host = find_and_maybe_start_agent_by_name_or_id(
        str(agent_name),
        agents_by_host,
        mngr_ctx,
        command_name="kanpan",
        skip_agent_state_check=True,
    )
    plugin_data = agent.get_plugin_data(PLUGIN_NAME)
    is_muted = not plugin_data.get("muted", False)
    plugin_data["muted"] = is_muted
    agent.set_plugin_data(PLUGIN_NAME, plugin_data)
    return is_muted


def _load_muted_agents(mngr_ctx: MngrContext) -> set[AgentName]:
    """Load the set of muted agent names from certified data."""
    muted: set[AgentName] = set()
    try:
        agents_by_host, _providers = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=None,
            agent_identifiers=None,
            include_destroyed=False,
            reset_caches=False,
        )
        for _host_ref, agent_refs in agents_by_host.items():
            for agent_ref in agent_refs:
                if _is_agent_muted(agent_ref.certified_data):
                    muted.add(agent_ref.agent_name)
    except Exception as e:
        logger.debug("Failed to load muted agents: {}", e)
    return muted


def _is_agent_muted(certified_data: Any) -> bool:
    """Check if an agent is muted based on its certified data."""
    return certified_data.get("plugin", {}).get(PLUGIN_NAME, {}).get("muted", False)


@pure
def _parse_github_repo_path(remote_url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL.

    Supports SSH (git@github.com:owner/repo.git) and
    HTTPS (https://github.com/owner/repo.git) formats.
    """
    # SSH format: git@github.com:owner/repo.git
    if remote_url.startswith("git@github.com:"):
        path = remote_url[len("git@github.com:") :]
        if path.endswith(".git"):
            path = path[:-4]
        return path

    # HTTPS format: https://github.com/owner/repo.git
    parsed = urlparse(remote_url)
    if parsed.hostname == "github.com":
        path = parsed.path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return path

    return None


@pure
def repo_path_from_labels(labels: dict[str, str]) -> str | None:
    """Extract GitHub 'owner/repo' from a labels dict's 'remote' entry."""
    remote_url = labels.get("remote")
    if remote_url is None:
        return None
    return _parse_github_repo_path(remote_url)


def collect_data_sources(
    mngr_ctx: MngrContext,
) -> list[KanpanDataSource]:
    """Collect all data sources from plugins and config.

    Calls pm.hook.kanpan_data_sources() to get plugin-registered sources,
    then filters by enabled status from config.
    """
    config = mngr_ctx.get_plugin_config("kanpan", KanpanPluginConfig)

    # Collect sources from hooks
    raw_results = mngr_ctx.pm.hook.kanpan_data_sources(mngr_ctx=mngr_ctx)

    all_sources: list[KanpanDataSource] = []
    for result in raw_results:
        if result is None:
            continue
        for source in result:
            all_sources.append(source)

    # Filter by enabled status in config
    enabled_sources: list[KanpanDataSource] = []
    for source in all_sources:
        source_config = config.data_sources.get(source.name)
        if source_config is not None:
            if isinstance(source_config, DataSourceConfig):
                if not source_config.enabled:
                    continue
            elif isinstance(source_config, dict):
                ds_config = DataSourceConfig(**source_config)
                if not ds_config.enabled:
                    continue
        enabled_sources.append(source)

    return enabled_sources
