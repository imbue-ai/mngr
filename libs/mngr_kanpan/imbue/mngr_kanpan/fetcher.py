import json
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import now_utc
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import CreatePrUrlField
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot

PLUGIN_NAME = "kanpan"


class FetchResult(FrozenModel):
    """Result of a fetch operation, carrying both the snapshot and new cached fields."""

    snapshot: BoardSnapshot = Field(description="The board snapshot")
    cached_fields: dict[AgentName, dict[str, FieldValue]] = Field(
        description="Updated cached fields for the next refresh cycle"
    )


def fetch_board_snapshot(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
    cached_fields: dict[AgentName, dict[str, FieldValue]],
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> FetchResult:
    """Full fetch: list agents, run data sources in parallel, build board entries.

    Cached fields from the previous cycle are passed in-memory (not persisted to disk).
    Returns a FetchResult with the snapshot and updated cached fields for the next cycle.
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

    # Run all data sources in parallel, passing cached fields from previous cycle
    new_fields_by_source, source_errors = _run_data_sources_parallel(data_sources, agents, cached_fields, mngr_ctx)
    errors.extend(source_errors)

    # Merge new fields into flat dict
    all_fields: dict[AgentName, dict[str, FieldValue]] = {}
    for _source_name, source_fields in new_fields_by_source.items():
        for agent_name, agent_fields in source_fields.items():
            if agent_name not in all_fields:
                all_fields[agent_name] = {}
            all_fields[agent_name].update(agent_fields)

    # Build board entries. The muted bit is sourced from local certified data
    # (always live), so its `created` is now.
    now = now_utc()
    entries: list[AgentBoardEntry] = []
    for agent in agents:
        agent_fields = dict(all_fields.get(agent.name, {}))
        is_muted = agent.name in muted_agents
        agent_fields[FIELD_MUTED] = BoolField(value=is_muted, created=now)

        cells = {key: field.display() for key, field in agent_fields.items()}
        section = compute_section(agent_fields)
        work_dir = _get_local_work_dir(agent)

        entries.append(
            AgentBoardEntry(
                name=agent.name,
                state=agent.state,
                provider_name=agent.host.provider_name,
                branch=agent.initial_branch,
                work_dir=work_dir,
                is_muted=is_muted,
                fields=agent_fields,
                cells=cells,
                section=section,
            )
        )

    elapsed = time.monotonic() - start_time
    snapshot = BoardSnapshot(
        entries=tuple(entries),
        errors=tuple(errors),
        fetch_time_seconds=elapsed,
    )
    return FetchResult(snapshot=snapshot, cached_fields=all_fields)


def fetch_local_snapshot(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
    cached_fields: dict[AgentName, dict[str, FieldValue]],
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> FetchResult:
    """Local-only snapshot: runs only non-remote data sources.

    Skips data sources with is_remote=True for speed.
    """
    local_sources = [s for s in data_sources if not s.is_remote]
    return fetch_board_snapshot(
        mngr_ctx,
        local_sources,
        cached_fields,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )


def _get_local_work_dir(agent: AgentDetails) -> Path | None:
    """Get the local work directory for an agent, if it exists."""
    if agent.host.provider_name == LOCAL_PROVIDER_NAME and agent.work_dir.exists():
        return agent.work_dir
    return None


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
    if isinstance(pr, CreatePrUrlField):
        # CreatePrUrlField in the pr slot means no real PR exists yet
        return BoardSection.STILL_COOKING
    if not isinstance(pr, PrField):
        raise KanpanFieldTypeError(f"Expected PrField for 'pr', got {type(pr).__name__}")

    match pr.state:
        case PrState.MERGED:
            return BoardSection.PR_MERGED
        case PrState.CLOSED:
            return BoardSection.PR_CLOSED
        case PrState.OPEN:
            if pr.is_draft:
                return BoardSection.PR_DRAFT
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
            raise AssertionError(f"Unhandled CI status: {ci.status}")
    raise AssertionError(f"Unhandled PR state: {pr.state}")


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


def _cache_file_path(mngr_ctx: MngrContext) -> Path:
    """Get the path to the kanpan field cache file."""
    return mngr_ctx.profile_dir / "kanpan" / "field_cache.json"


def save_field_cache(
    mngr_ctx: MngrContext,
    cached_fields: dict[AgentName, dict[str, FieldValue]],
) -> None:
    """Persist cached fields to a local JSON file atomically.

    Writes a temporary file then renames it to avoid partial reads.
    Each field is stored as {field_key: {type: class_name, data: model_dump}}.
    """
    cache_path = _cache_file_path(mngr_ctx)
    tmp_path: str | None = None
    try:
        serialized: dict[str, dict[str, Any]] = {}
        for agent_name, agent_fields in cached_fields.items():
            agent_data: dict[str, Any] = {}
            for key, field in agent_fields.items():
                # mode='json' emits JSON-native primitives (datetime -> ISO string).
                agent_data[key] = {
                    "type": type(field).__name__,
                    "data": field.model_dump(mode="json"),
                }
            serialized[str(agent_name)] = agent_data

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=cache_path.parent, suffix=".tmp")
        with open(fd, "w") as f:
            json.dump(serialized, f)
        Path(tmp_path).rename(cache_path)
        tmp_path = None
    except Exception as e:
        logger.debug("Failed to save field cache: {}", e)
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


def load_field_cache(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
) -> dict[AgentName, dict[str, FieldValue]]:
    """Load cached fields from the local JSON file.

    Uses field_types from data sources to deserialize each field value.
    Returns an empty dict if the cache file doesn't exist or is corrupt.
    """
    cache_path = _cache_file_path(mngr_ctx)
    if not cache_path.exists():
        return {}

    # Build type registry from all data sources
    type_registry: dict[str, type[FieldValue]] = {}
    for source in data_sources:
        for _key, field_type in source.field_types.items():
            type_registry[field_type.__name__] = field_type

    try:
        raw = json.loads(cache_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.debug("Failed to load field cache: {}", e)
        return {}

    # Per-field validation failures (e.g. legacy entries missing `created` after
    # the staleness migration) are tolerated: drop the bad entry and keep the
    # rest. The cache is best-effort and will be rebuilt on the next refresh.
    result: dict[AgentName, dict[str, FieldValue]] = {}
    for agent_name_str, agent_data in raw.items():
        agent_fields: dict[str, FieldValue] = {}
        for key, field_info in agent_data.items():
            type_name = field_info.get("type")
            data = field_info.get("data")
            field_type = type_registry.get(type_name or "")
            if field_type is None or data is None:
                continue
            try:
                agent_fields[key] = field_type.model_validate(data)
            except ValidationError as e:
                logger.debug(
                    "Skipping invalid cached field '{}' for agent '{}': {}",
                    key,
                    agent_name_str,
                    e,
                )
        if agent_fields:
            result[AgentName(agent_name_str)] = agent_fields
    return result


def collect_data_sources(
    mngr_ctx: MngrContext,
) -> list[KanpanDataSource]:
    """Collect all data sources from plugins.

    Plugins are responsible for checking their own enabled status before
    returning sources (see plugin.py's _is_source_enabled).
    """
    raw_results = mngr_ctx.pm.hook.kanpan_data_sources(mngr_ctx=mngr_ctx)

    sources: list[KanpanDataSource] = []
    for result in raw_results:
        if result is None:
            continue
        for source in result:
            sources.append(source)

    return sources
