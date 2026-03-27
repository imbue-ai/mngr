from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from threading import Lock
from typing import Any

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.discover import warn_on_duplicate_host_names
from imbue.mngr.api.discovery_events import emit_host_ssh_info
from imbue.mngr.api.discovery_events import extract_agents_and_hosts_from_full_listing
from imbue.mngr.api.discovery_events import write_full_discovery_snapshot
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderInstanceNotFoundError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.cel_utils import compile_cel_filters


class ErrorInfo(FrozenModel):
    """Information about an error encountered during listing.

    This preserves the exception type and message instead of converting to a string immediately.
    """

    exception_type: str = Field(description="The type name of the exception (e.g., 'RuntimeError')")
    message: str = Field(description="The error message")

    @classmethod
    def build(cls, exception: BaseException) -> "ErrorInfo":
        """Build an ErrorInfo from an exception."""
        return cls(exception_type=type(exception).__name__, message=str(exception))


class ProviderErrorInfo(ErrorInfo):
    """Error information with provider context."""

    provider_name: ProviderInstanceName = Field(description="Name of the provider where the error occurred")

    @classmethod
    def build_for_provider(cls, exception: BaseException, provider_name: ProviderInstanceName) -> "ProviderErrorInfo":
        """Build a ProviderErrorInfo from an exception and provider name."""
        return cls(
            exception_type=type(exception).__name__,
            message=str(exception),
            provider_name=provider_name,
        )


class HostErrorInfo(ErrorInfo):
    """Error information with host context."""

    host_id: HostId = Field(description="ID of the host where the error occurred")

    @classmethod
    def build_for_host(cls, exception: BaseException, host_id: HostId) -> "HostErrorInfo":
        """Build a HostErrorInfo from an exception and host ID."""
        return cls(
            exception_type=type(exception).__name__,
            message=str(exception),
            host_id=host_id,
        )


class AgentErrorInfo(ErrorInfo):
    """Error information with agent context."""

    agent_id: AgentId = Field(description="ID of the agent where the error occurred")

    @classmethod
    def build_for_agent(cls, exception: BaseException, agent_id: AgentId) -> "AgentErrorInfo":
        """Build an AgentErrorInfo from an exception and agent ID."""
        return cls(
            exception_type=type(exception).__name__,
            message=str(exception),
            agent_id=agent_id,
        )


class ListResult(MutableModel):
    """Result of listing agents."""

    agents: list[AgentDetails] = Field(default_factory=list, description="List of agents with their full information")
    errors: list[ErrorInfo] = Field(default_factory=list, description="Errors encountered while listing")


class _ListAgentsParams(FrozenModel):
    """Shared parameters for the internal agent listing pipeline."""

    model_config = {"arbitrary_types_allowed": True}
    compiled_include_filters: list[Any]
    compiled_exclude_filters: list[Any]
    error_behavior: ErrorBehavior
    on_agent: Callable[[AgentDetails], None] | None
    on_error: Callable[[ErrorInfo], None] | None
    field_generators: dict[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] = Field(
        default_factory=dict,
    )


@log_call
def list_agents(
    mngr_ctx: MngrContext,
    # When True, each provider streams results as soon as it finishes loading
    # (on_agent fires immediately per provider, without waiting for all providers)
    is_streaming: bool,
    # CEL expressions - only include agents matching these
    include_filters: tuple[str, ...] = (),
    # CEL expressions - exclude agents matching these
    exclude_filters: tuple[str, ...] = (),
    # If specified, only list agents from these providers (NOT IMPLEMENTED YET)
    provider_names: tuple[str, ...] | None = None,
    # How to handle errors (abort or continue)
    error_behavior: ErrorBehavior = ErrorBehavior.ABORT,
    # Optional callback invoked immediately when each agent is found (for streaming)
    on_agent: Callable[[AgentDetails], None] | None = None,
    # Optional callback invoked immediately when each error is encountered (for streaming)
    on_error: Callable[[ErrorInfo], None] | None = None,
    # whether to force the providers to refresh their caches and get new data. Only needed if calling this multiple
    # times within the same process
    reset_caches: bool = False,
) -> ListResult:
    """List all agents with optional filtering."""
    result = ListResult()

    # Compile CEL filters if provided
    # Note: compilation errors always abort - bad filters should never silently continue
    compiled_include_filters: list[Any] = []
    compiled_exclude_filters: list[Any] = []
    if include_filters or exclude_filters:
        with log_span("Compiling CEL filters", include_filters=include_filters, exclude_filters=exclude_filters):
            compiled_include_filters, compiled_exclude_filters = compile_cel_filters(include_filters, exclude_filters)

    try:
        results_lock = Lock()

        field_generators: dict[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] = {}
        for hook_result in mngr_ctx.pm.hook.agent_field_generators():
            if hook_result is not None:
                plugin_name, generators = hook_result
                field_generators[plugin_name] = generators

        params = _ListAgentsParams(
            compiled_include_filters=compiled_include_filters,
            compiled_exclude_filters=compiled_exclude_filters,
            error_behavior=error_behavior,
            on_agent=on_agent,
            on_error=on_error,
            field_generators=field_generators,
        )

        if is_streaming:
            # Streaming mode: each provider loads hosts, gets agent refs, and processes
            # hosts immediately -- so fast providers fire on_agent callbacks while slow
            # providers are still loading
            _list_agents_streaming(
                mngr_ctx=mngr_ctx,
                provider_names=provider_names,
                params=params,
                result=result,
                results_lock=results_lock,
                reset_caches=reset_caches,
            )
        else:
            # Batch mode: load all agents first, then process
            _list_agents_batch(
                mngr_ctx=mngr_ctx,
                provider_names=provider_names,
                params=params,
                result=result,
                results_lock=results_lock,
                reset_caches=reset_caches,
            )

    except MngrError as e:
        if error_behavior == ErrorBehavior.ABORT:
            raise
        error_info = ErrorInfo.build(e)
        result.errors.append(error_info)
        if on_error:
            on_error(error_info)

    _maybe_write_full_discovery_snapshot(mngr_ctx, result, provider_names, include_filters, exclude_filters)
    return result


def _maybe_write_full_discovery_snapshot(
    mngr_ctx: MngrContext,
    result: ListResult,
    provider_names: tuple[str, ...] | None,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
) -> None:
    """Write a full discovery snapshot when this listing represents all known agents.

    A snapshot is written only when the listing is complete and error-free:
    - All providers were queried (no provider_names filter)
    - No CEL filters were applied (the result contains every agent)
    - No errors occurred during listing (otherwise we may be missing agents)
    """
    is_full_listing = provider_names is None and not include_filters and not exclude_filters
    if not is_full_listing:
        return
    if result.errors:
        logger.trace("Skipping full discovery snapshot: {} error(s) during listing", len(result.errors))
        return
    try:
        discovered_agents, discovered_hosts, host_ssh_infos = extract_agents_and_hosts_from_full_listing(result.agents)
        write_full_discovery_snapshot(mngr_ctx.config, discovered_agents, discovered_hosts)
        for host_id, ssh_info in host_ssh_infos:
            emit_host_ssh_info(mngr_ctx.config, host_id, ssh_info)
    except (MngrError, OSError) as e:
        logger.warning("Failed to write full discovery snapshot: {}", e)


def _list_agents_batch(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
    reset_caches: bool = False,
) -> None:
    """Batch mode: load all agents from all providers, then process hosts."""
    with log_span("Loading agents from all providers"):
        agents_by_host, providers = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=provider_names,
            agent_identifiers=None,
            include_destroyed=True,
            reset_caches=reset_caches,
        )
    provider_map = {provider.name: provider for provider in providers}
    logger.trace("Found {} hosts with agents", len(agents_by_host))

    # Process each host and its agents in parallel
    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group, name="list_agents_process_hosts", max_workers=32
    ) as executor:
        for host_ref, agent_refs in agents_by_host.items():
            if not agent_refs:
                continue

            provider = provider_map.get(host_ref.provider_name)
            if not provider:
                exception = ProviderInstanceNotFoundError(host_ref.provider_name)
                if params.error_behavior == ErrorBehavior.ABORT:
                    raise exception
                error_info = ProviderErrorInfo.build_for_provider(exception, host_ref.provider_name)
                with results_lock:
                    result.errors.append(error_info)
                if params.on_error:
                    params.on_error(error_info)
                continue

            futures.append(
                executor.submit(
                    _process_host_with_error_handling,
                    host_ref,
                    agent_refs,
                    provider,
                    params,
                    result,
                    results_lock,
                )
            )

    # Re-raise any thread exceptions (e.g. abort-mode errors)
    for future in futures:
        future.result()


def _list_agents_streaming(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
    reset_caches: bool = False,
) -> None:
    """Streaming mode: each provider loads and processes hosts independently.

    Fast providers fire on_agent callbacks while slow providers are still loading.
    """
    with log_span("Loading agents from all providers (streaming)"):
        providers = get_all_provider_instances(mngr_ctx, provider_names, reset_caches=reset_caches)
        logger.trace("Found {} provider instances", len(providers))

        with ConcurrencyGroupExecutor(
            parent_cg=mngr_ctx.concurrency_group, name="list_agents_streaming", max_workers=32
        ) as executor:
            streaming_futures: list[Future[None]] = []
            for provider in providers:
                streaming_futures.append(
                    executor.submit(
                        _discover_and_emit_details_for_provider,
                        provider,
                        params,
                        result,
                        results_lock,
                        mngr_ctx.concurrency_group,
                    )
                )

        # Re-raise any thread exceptions
        for future in streaming_futures:
            future.result()


def _discover_and_emit_details_for_provider(
    provider: BaseProviderInstance,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
    cg: ConcurrencyGroup,
) -> None:
    """Load hosts from a single provider, get agent refs, and immediately process them.

    This is the streaming counterpart to the batch approach. Each provider independently
    loads hosts, fetches agent references, then processes hosts -- firing on_agent callbacks
    without waiting for other providers.
    """
    try:
        # Phase 1: list hosts and get agent refs
        provider_results = provider.discover_hosts_and_agents(cg=cg, include_destroyed=True)

        # Warn if any host names are duplicated within this provider
        warn_on_duplicate_host_names(provider_results)

        # Phase 2: immediately process hosts (fire on_agent for this provider)
        host_futures: list[Future[None]] = []
        with ConcurrencyGroupExecutor(parent_cg=cg, name=f"stream_hosts_{provider.name}", max_workers=32) as executor:
            for host_ref, agent_refs in provider_results.items():
                if not agent_refs:
                    continue

                host_futures.append(
                    executor.submit(
                        _process_host_with_error_handling,
                        host_ref,
                        agent_refs,
                        provider,
                        params,
                        result,
                        results_lock,
                    )
                )

        # Re-raise any thread exceptions
        for future in host_futures:
            future.result()

    except MngrError as e:
        if params.error_behavior == ErrorBehavior.ABORT:
            raise
        error_info = ProviderErrorInfo.build_for_provider(e, provider.name)
        with results_lock:
            result.errors.append(error_info)
        if params.on_error:
            params.on_error(error_info)


def _handle_listing_error(
    source: DiscoveredAgent | DiscoveredHost,
    exception: BaseException,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
) -> None:
    """Handle an error during detail collection for an agent or host."""
    if params.error_behavior == ErrorBehavior.ABORT:
        raise exception
    if isinstance(source, DiscoveredAgent):
        error_info = AgentErrorInfo.build_for_agent(exception, source.agent_id)
    else:
        error_info = HostErrorInfo.build_for_host(exception, source.host_id)
    with results_lock:
        result.errors.append(error_info)
    if params.on_error:
        params.on_error(error_info)


def _collect_and_emit_details_for_host(
    host_ref: DiscoveredHost,
    agent_refs: list[DiscoveredAgent],
    provider: ProviderInstanceInterface,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
) -> None:
    _host_details, agent_details_list = provider.get_host_and_agent_details(
        host_ref,
        agent_refs,
        params.field_generators,
        lambda source, exc: _handle_listing_error(source, exc, params, result, results_lock),
    )
    for agent_details in agent_details_list:
        # Apply CEL filters if provided
        if params.compiled_include_filters or params.compiled_exclude_filters:
            if not _apply_cel_filters(agent_details, params.compiled_include_filters, params.compiled_exclude_filters):
                continue
        with results_lock:
            result.agents.append(agent_details)
        if params.on_agent:
            params.on_agent(agent_details)


def _process_host_with_error_handling(
    host_ref: DiscoveredHost,
    agent_refs: list[DiscoveredAgent],
    provider: ProviderInstanceInterface,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
) -> None:
    """Process a single host and collect its agents.

    This function is run in a thread by list_agents.
    Results are merged into the shared result object under the results_lock.
    """
    try:
        _collect_and_emit_details_for_host(
            host_ref,
            agent_refs,
            provider,
            params,
            result,
            results_lock,
        )

    except (MngrError, BaseMngrError) as e:
        if params.error_behavior == ErrorBehavior.ABORT:
            raise
        error_info = HostErrorInfo.build_for_host(e, host_ref.host_id)
        with results_lock:
            result.errors.append(error_info)
        if params.on_error:
            params.on_error(error_info)


@pure
def agent_details_to_cel_context(agent: AgentDetails) -> dict[str, Any]:
    """Convert an AgentDetails object to a CEL-friendly dict.

    Converts the agent into a flat dictionary suitable for CEL evaluation,
    adding computed fields and type information.
    """
    result = agent.model_dump(mode="json")

    # Add age from create_time
    if result.get("create_time"):
        if isinstance(result["create_time"], str):
            created_dt = datetime.fromisoformat(result["create_time"].replace("Z", "+00:00"))
        else:
            created_dt = result["create_time"]
        result["age"] = (datetime.now(timezone.utc) - created_dt).total_seconds()

    # Add runtime_seconds if available
    if result.get("runtime_seconds") is not None:
        result["runtime"] = result["runtime_seconds"]

    # Add idle_seconds if available (computed from activity times)
    if result.get("user_activity_time") or result.get("agent_activity_time"):
        latest_activity = None
        for activity_field in ["user_activity_time", "agent_activity_time", "ssh_activity_time"]:
            activity_time = result.get(activity_field)
            if activity_time:
                if isinstance(activity_time, str):
                    activity_dt = datetime.fromisoformat(activity_time.replace("Z", "+00:00"))
                else:
                    activity_dt = activity_time
                if latest_activity is None or activity_dt > latest_activity:
                    latest_activity = activity_dt
        if latest_activity:
            result["idle"] = (datetime.now(timezone.utc) - latest_activity).total_seconds()

    # Normalize host.provider_name to host.provider for consistency
    if result.get("host") and isinstance(result["host"], dict):
        host = result["host"]
        if "provider_name" in host:
            host["provider"] = host.pop("provider_name")

    return result


def _apply_cel_filters(
    agent: AgentDetails,
    include_filters: Sequence[Any],
    exclude_filters: Sequence[Any],
) -> bool:
    """Apply CEL filters to an agent.

    Returns True if the agent should be included (matches all include filters
    and doesn't match any exclude filters).
    """
    context = agent_details_to_cel_context(agent)
    return apply_cel_filters_to_context(
        context=context,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        error_context_description=f"agent {agent.name}",
    )
