import json
import tempfile
import time
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field
from pydantic import TypeAdapter

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import PLUGIN_NAME
from imbue.mngr_kanpan.data_source import deserialize_fields
from imbue.mngr_kanpan.data_source import is_muted
from imbue.mngr_kanpan.data_source import now_utc
from imbue.mngr_kanpan.data_sources.github import CreatePrUrlField
from imbue.mngr_kanpan.data_sources.github import PrFetchFailedField
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot


class FetchResult(FrozenModel):
    """Result of a fetch operation, carrying both the snapshot and new cached fields."""

    snapshot: BoardSnapshot = Field(description="The board snapshot")
    cached_fields: dict[AgentName, dict[str, FieldValue]] = Field(
        description="Updated cached fields for the next refresh cycle"
    )


class FetchUpdate(FrozenModel):
    """One incremental update posted by a streaming refresh.

    ``stream_board_snapshot`` posts a sequence of these as its data sources
    return. Each non-error update carries a full board ``snapshot`` that is
    strictly more complete than the previous one (fresh source output layered
    over the last-known cached values), so a consumer can simply render the
    latest one it has drained.

    ``cached_fields`` holds only the freshly computed fields -- never the seeded
    cache values -- so the final update's ``cached_fields`` matches what a batch
    ``fetch_board_snapshot`` would produce and is what gets persisted.
    """

    snapshot: BoardSnapshot | None = Field(
        default=None, description="Full board snapshot to render; None only on a hard failure."
    )
    cached_fields: dict[AgentName, dict[str, FieldValue]] = Field(
        default_factory=dict,
        description="Freshly computed (unseeded) fields so far; authoritative on the final update.",
    )
    is_final: bool = Field(default=False, description="True on the last update of the stream.")
    error: str | None = Field(
        default=None, description="Hard-failure detail; set only on a final update whose fetch crashed."
    )


def _assemble_entries(
    agents: tuple[AgentDetails, ...],
    all_fields: dict[AgentName, dict[str, FieldValue]],
) -> tuple[AgentBoardEntry, ...]:
    """Build board entries from agents and their computed fields.

    The muted bit rides on each AgentDetails (populated by kanpan's
    agent_field_generators / offline_agent_field_generators during the
    list_agents call), so it is sourced as resiliently as the agent list itself
    and its ``created`` is now.
    """
    now = now_utc()
    entries: list[AgentBoardEntry] = []
    for agent in agents:
        agent_fields = dict(all_fields.get(agent.name, {}))
        is_agent_muted = is_muted(agent.plugin.get(PLUGIN_NAME, {}))
        agent_fields[FIELD_MUTED] = BoolField(value=is_agent_muted, created=now)

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
                is_muted=is_agent_muted,
                fields=agent_fields,
                cells=cells,
                section=section,
            )
        )
    return tuple(entries)


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

    entries = _assemble_entries(agents, all_fields)

    elapsed = time.monotonic() - start_time
    snapshot = BoardSnapshot(
        entries=entries,
        errors=tuple(errors),
        fetch_time_seconds=elapsed,
    )
    return FetchResult(snapshot=snapshot, cached_fields=all_fields)


def stream_board_snapshot(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
    cached_fields: dict[AgentName, dict[str, FieldValue]],
    post: Callable[[FetchUpdate], None],
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> None:
    """Streaming full fetch: list agents, then fan out data sources, posting a
    progressively more complete snapshot as each source returns.

    Meant to run in a background thread. ``post`` is called once with a base
    snapshot (agents in their last-known sections, seeded from ``cached_fields``
    and rendered stale by their age), then once per data source as it returns,
    and a final time with ``is_final=True``. On an unexpected failure a single
    final update carrying ``error`` is posted instead, leaving the current board
    for the consumer to preserve and retry.

    The seeded cache values are display-only: ``cached_fields`` on each posted
    update carries just the freshly computed fields, and once a source returns
    its field keys are governed entirely by that fresh output (a stale seeded
    value for one of its keys is dropped, even if the source failed and produced
    nothing), so the final snapshot is identical to what ``fetch_board_snapshot``
    would produce.
    """
    start_time = time.monotonic()
    try:
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

        # `display_fields` starts seeded from the previous cycle's cache so agents
        # appear in their last-known sections immediately; the old `created` on
        # those values renders them stale until fresh data lands. `fresh_fields`
        # accumulates only freshly computed values and becomes the persisted cache.
        display_fields: dict[AgentName, dict[str, FieldValue]] = {
            agent.name: dict(cached_fields.get(agent.name, {})) for agent in agents
        }
        fresh_fields: dict[AgentName, dict[str, FieldValue]] = {}

        if not data_sources:
            _post_stream_update(post, agents, display_fields, fresh_fields, errors, start_time, is_final=True)
            return

        # Base snapshot: every agent visible at once, before any source returns.
        _post_stream_update(post, agents, display_fields, fresh_fields, errors, start_time, is_final=False)

        with ThreadPoolExecutor(max_workers=min(len(data_sources), 8)) as executor:
            future_to_source: dict[
                Future[tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]], KanpanDataSource
            ] = {
                executor.submit(_compute_source_safely, source, agents, cached_fields, mngr_ctx): source
                for source in data_sources
            }
            remaining = len(future_to_source)
            for future in as_completed(future_to_source):
                source = future_to_source[future]
                source_fields, source_errors = future.result()
                errors.extend(source_errors)
                # This source's keys are now authoritative: drop any seeded value
                # for them (even where the source produced nothing for an agent),
                # then layer in the fresh output.
                owned_keys = set(source.field_types.keys())
                for agent_fields in display_fields.values():
                    for key in owned_keys:
                        agent_fields.pop(key, None)
                for agent_name, agent_fields in source_fields.items():
                    display_fields.setdefault(agent_name, {}).update(agent_fields)
                    fresh_fields.setdefault(agent_name, {}).update(agent_fields)
                remaining -= 1
                _post_stream_update(
                    post, agents, display_fields, fresh_fields, errors, start_time, is_final=(remaining == 0)
                )
    except Exception as e:
        logger.debug("Streaming refresh failed: {}", e)
        post(FetchUpdate(error=f"Refresh failed: {e}", is_final=True))


def stream_local_snapshot(
    mngr_ctx: MngrContext,
    data_sources: Sequence[KanpanDataSource],
    cached_fields: dict[AgentName, dict[str, FieldValue]],
    post: Callable[[FetchUpdate], None],
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> None:
    """Streaming local-only fetch: like ``stream_board_snapshot`` but runs only
    non-remote sources.

    Remote fields (PR, CI, ...) are carried forward implicitly through the
    seeded cache: no remote source runs to claim their keys, so their last-known
    values stay on the board (rendered stale by age) instead of being dropped.
    """
    local_sources = [s for s in data_sources if not s.is_remote]
    stream_board_snapshot(
        mngr_ctx,
        local_sources,
        cached_fields,
        post,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )


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


def _compute_source_safely(
    source: KanpanDataSource,
    agents: tuple[AgentDetails, ...],
    cached_fields: dict[AgentName, dict[str, FieldValue]],
    mngr_ctx: MngrContext,
) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
    """Run one data source's ``compute``, turning any failure into an error string.

    Data sources are pluggable, so a single misbehaving source must never crash
    the refresh (batch or streaming). A failure is surfaced as an error message
    and the source contributes no fields, exactly as if it had returned empty.
    The broad catch is intentional: a source can raise anything, and none of it
    should take down the board.
    """
    try:
        return source.compute(agents, cached_fields, mngr_ctx)
    except Exception as e:
        logger.debug("Data source '{}' failed: {}", source.name, e)
        return {}, [f"Data source '{source.name}' failed: {e}"]


def _post_stream_update(
    post: Callable[[FetchUpdate], None],
    agents: tuple[AgentDetails, ...],
    display_fields: dict[AgentName, dict[str, FieldValue]],
    fresh_fields: dict[AgentName, dict[str, FieldValue]],
    errors: Sequence[str],
    start_time: float,
    is_final: bool,
) -> None:
    """Assemble a board snapshot from the current display fields and post it.

    ``display_fields`` drives what the board shows (seed layered under fresh
    output); ``fresh_fields`` -- only the freshly computed values -- rides along
    as ``cached_fields`` so the final update carries the authoritative cache.
    """
    entries = _assemble_entries(agents, display_fields)
    elapsed = time.monotonic() - start_time
    snapshot = BoardSnapshot(entries=entries, errors=tuple(errors), fetch_time_seconds=elapsed)
    cached_copy = {name: dict(fields) for name, fields in fresh_fields.items()}
    post(FetchUpdate(snapshot=snapshot, cached_fields=cached_copy, is_final=is_final))


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
            futures[source.name] = executor.submit(_compute_source_safely, source, agents, cached_fields, mngr_ctx)

        for source_name, future in futures.items():
            source_fields, source_errors = future.result()
            results[source_name] = source_fields
            all_errors.extend(source_errors)

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
    if isinstance(pr, PrFetchFailedField):
        # The repo's PR fetch failed and no cached PrField was available to
        # fall back to, so we genuinely have no PR data for this agent.
        return BoardSection.PRS_FAILED
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
            return BoardSection.PR_BEING_REVIEWED
    raise AssertionError(f"Unhandled PR state: {pr.state}")


def toggle_agent_mute(mngr_ctx: MngrContext, agent_name: AgentName) -> bool:
    """Toggle the mute state of an agent. Returns the new mute state."""
    host_ref, agent_ref = find_one_agent(AgentAddress(agent=agent_name), mngr_ctx)
    agent, _host = resolve_to_started_host_and_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=False,
        mngr_ctx=mngr_ctx,
    )
    plugin_data = agent.get_plugin_data(PLUGIN_NAME)
    is_agent_muted = not plugin_data.get(FIELD_MUTED, False)
    plugin_data[FIELD_MUTED] = is_agent_muted
    agent.set_plugin_data(PLUGIN_NAME, plugin_data)
    return is_agent_muted


def _cache_file_path(mngr_ctx: MngrContext) -> Path:
    """Get the path to the kanpan field cache file."""
    return mngr_ctx.profile_dir / "kanpan" / "field_cache.json"


def save_field_cache(
    mngr_ctx: MngrContext,
    cached_fields: dict[AgentName, dict[str, FieldValue]],
) -> None:
    """Persist cached fields to a local JSON file atomically.

    Writes a temporary file then renames it to avoid partial reads.
    Each field is stored as ``{field_key: model_dump}`` -- the dump
    includes the FieldValue subclass's ``kind`` discriminator, so no
    separate type envelope is needed.
    """
    cache_path = _cache_file_path(mngr_ctx)
    tmp_path: str | None = None
    try:
        serialized: dict[str, dict[str, Any]] = {}
        for agent_name, agent_fields in cached_fields.items():
            agent_data: dict[str, Any] = {}
            for key, field in agent_fields.items():
                # mode="json" emits JSON-native primitives (datetime -> ISO
                # string, enums -> str). The dump includes the FieldValue
                # subclass's `kind` discriminator, so no separate type
                # envelope is needed -- load_field_cache rehydrates via the
                # discriminated-union TypeAdapter.
                agent_data[key] = field.model_dump(mode="json")
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

    Each slot's TypeAdapter (from ``KanpanDataSource.field_types``) validates
    the raw payload. For polymorphic slots the adapter wraps a discriminated
    union and dispatches on the ``kind`` tag in the payload. Returns an empty
    dict if the cache file doesn't exist or is corrupt; per-key validation
    failures are logged at debug and the offending key is dropped (see
    ``deserialize_fields``).
    """
    cache_path = _cache_file_path(mngr_ctx)
    if not cache_path.exists():
        return {}

    adapters: dict[str, TypeAdapter[FieldValue]] = {}
    for source in data_sources:
        adapters.update(source.field_types)

    try:
        raw = json.loads(cache_path.read_text())
        result: dict[AgentName, dict[str, FieldValue]] = {}
        for agent_name_str, agent_data in raw.items():
            # deserialize_fields drops per-key ValidationErrors with a debug
            # log (covers the legacy-entries-missing-`created` migration case).
            agent_fields = deserialize_fields(agent_data, adapters)
            if agent_fields:
                result[AgentName(agent_name_str)] = agent_fields
        return result
    except Exception as e:
        # Tolerate any read/parse/decode failure and behave as the docstring
        # promises ("returns empty dict if the cache file doesn't exist or
        # is corrupt"). The broad catch is intentional and covers
        # UnicodeDecodeError from partial-write corruption alongside the
        # narrower OSError / json.JSONDecodeError cases.
        logger.debug("Failed to load field cache: {}", e)
        return {}


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
