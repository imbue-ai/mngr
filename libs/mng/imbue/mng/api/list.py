from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from pathlib import Path
from threading import Lock
from typing import Any

from loguru import logger
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.api.providers import get_all_provider_instances
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundOnHostError
from imbue.mng.errors import HostAuthenticationError
from imbue.mng.errors import HostConnectionError
from imbue.mng.errors import MngError
from imbue.mng.errors import ProviderInstanceNotFoundError
from imbue.mng.hosts.common import compute_idle_seconds
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.interfaces.data_types import SSHInfo
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostReference
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.utils.cel_utils import apply_cel_filters_to_context
from imbue.mng.utils.cel_utils import compile_cel_filters
from imbue.mng.utils.completion_writer import get_completion_cache_dir
from imbue.mng.utils.completion_writer import write_agent_names_cache


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

    agents: list[AgentInfo] = Field(default_factory=list, description="List of agents with their full information")
    errors: list[ErrorInfo] = Field(default_factory=list, description="Errors encountered while listing")


class _ListAgentsParams(FrozenModel):
    """Shared parameters for the internal agent listing pipeline.

    Bundles the filter/callback parameters that are threaded through
    _list_agents_batch, _list_agents_streaming, _process_provider_streaming,
    _process_host_for_agent_listing, and _assemble_host_info.
    """

    model_config = {"arbitrary_types_allowed": True}
    compiled_include_filters: list[Any]
    compiled_exclude_filters: list[Any]
    error_behavior: ErrorBehavior
    on_agent: Callable[[AgentInfo], None] | None
    on_error: Callable[[ErrorInfo], None] | None


@log_call
def list_agents(
    mng_ctx: MngContext,
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
    on_agent: Callable[[AgentInfo], None] | None = None,
    # Optional callback invoked immediately when each error is encountered (for streaming)
    on_error: Callable[[ErrorInfo], None] | None = None,
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
        params = _ListAgentsParams(
            compiled_include_filters=compiled_include_filters,
            compiled_exclude_filters=compiled_exclude_filters,
            error_behavior=error_behavior,
            on_agent=on_agent,
            on_error=on_error,
        )

        if is_streaming:
            # Streaming mode: each provider loads hosts, gets agent refs, and processes
            # hosts immediately -- so fast providers fire on_agent callbacks while slow
            # providers are still loading
            _list_agents_streaming(
                mng_ctx=mng_ctx,
                provider_names=provider_names,
                params=params,
                result=result,
                results_lock=results_lock,
            )
        else:
            # Batch mode: load all agents first, then process
            _list_agents_batch(
                mng_ctx=mng_ctx,
                provider_names=provider_names,
                params=params,
                result=result,
                results_lock=results_lock,
            )

    except MngError as e:
        if error_behavior == ErrorBehavior.ABORT:
            raise
        error_info = ErrorInfo.build(e)
        result.errors.append(error_info)
        if on_error:
            on_error(error_info)

    agent_names = [str(agent.name) for agent in result.agents]
    write_agent_names_cache(get_completion_cache_dir(), agent_names)

    return result


def _list_agents_batch(
    mng_ctx: MngContext,
    provider_names: tuple[str, ...] | None,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
) -> None:
    """Batch mode: load all agents from all providers, then process hosts."""
    with log_span("Loading agents from all providers"):
        agents_by_host, providers = load_all_agents_grouped_by_host(mng_ctx, provider_names, include_destroyed=True)
    provider_map = {provider.name: provider for provider in providers}
    logger.trace("Found {} hosts with agents", len(agents_by_host))

    # Process each host and its agents in parallel
    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mng_ctx.concurrency_group, name="list_agents_process_hosts", max_workers=32
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
                    _process_host_for_agent_listing,
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
    mng_ctx: MngContext,
    provider_names: tuple[str, ...] | None,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
) -> None:
    """Streaming mode: each provider loads and processes hosts independently.

    Fast providers fire on_agent callbacks while slow providers are still loading.
    """
    with log_span("Loading agents from all providers (streaming)"):
        providers = get_all_provider_instances(mng_ctx, provider_names)
        logger.trace("Found {} provider instances", len(providers))

        with ConcurrencyGroupExecutor(
            parent_cg=mng_ctx.concurrency_group, name="list_agents_streaming", max_workers=32
        ) as executor:
            streaming_futures: list[Future[None]] = []
            for provider in providers:
                streaming_futures.append(
                    executor.submit(
                        _process_provider_streaming,
                        provider,
                        params,
                        result,
                        results_lock,
                        mng_ctx.concurrency_group,
                    )
                )

        # Re-raise any thread exceptions
        for future in streaming_futures:
            future.result()


def _process_provider_streaming(
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
        provider_results = provider.load_agent_refs(cg=cg, include_destroyed=True)

        # Warn if any host names are duplicated within this provider
        _warn_on_duplicate_host_names(provider_results)

        # Phase 2: immediately process hosts (fire on_agent for this provider)
        host_futures: list[Future[None]] = []
        with ConcurrencyGroupExecutor(parent_cg=cg, name=f"stream_hosts_{provider.name}", max_workers=32) as executor:
            for host_ref, agent_refs in provider_results.items():
                if not agent_refs:
                    continue

                host_futures.append(
                    executor.submit(
                        _process_host_for_agent_listing,
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

    except MngError as e:
        if params.error_behavior == ErrorBehavior.ABORT:
            raise
        error_info = ProviderErrorInfo.build_for_provider(e, provider.name)
        with results_lock:
            result.errors.append(error_info)
        if params.on_error:
            params.on_error(error_info)


# retry exactly once if there is a HostConnectionError (hopefully we then simply load the offline version of the host)
@retry(
    retry=retry_if_exception_type(HostConnectionError),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _assemble_host_info(
    host_ref: HostReference,
    agent_refs: list[AgentReference],
    provider: ProviderInstanceInterface,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
) -> None:
    is_authentication_failure = False
    try:
        # Try the provider's optimized listing method first
        listing_data = provider.build_host_listing_data(host_ref, agent_refs)
        if listing_data is not None:
            host_info, agent_infos = listing_data
            for agent_info in agent_infos:
                # Apply CEL filters if provided
                if params.compiled_include_filters or params.compiled_exclude_filters:
                    if not _apply_cel_filters(
                        agent_info, params.compiled_include_filters, params.compiled_exclude_filters
                    ):
                        continue
                with results_lock:
                    result.agents.append(agent_info)
                if params.on_agent:
                    params.on_agent(agent_info)
            return

        # Fall back to per-field collection
        # get the host
        host = provider.get_host(host_ref.host_id)
    except HostAuthenticationError:
        host = provider.to_offline_host(host_ref.host_id)
        is_authentication_failure = True

    # Build SSH info if this is a remote host (only available for online hosts)
    ssh_info: SSHInfo | None = None

    # Host is the implementation of OnlineHostInterface, ie, this host is online
    is_locked: bool | None = None
    locked_time: datetime | None = None
    if isinstance(host, Host):
        ssh_connection = host._get_ssh_connection_info()
        if ssh_connection is not None:
            user, hostname, port, key_path = ssh_connection
            ssh_info = SSHInfo(
                user=user,
                host=hostname,
                port=port,
                key_path=key_path,
                command=f"ssh -i {key_path} -p {port} {user}@{hostname}",
            )
        boot_time = host.get_boot_time()
        uptime_seconds = host.get_uptime_seconds()
        resource = host.get_provider_resources()
        is_locked = host.is_lock_held()
        # Only fetch locked_time when the lock is held to avoid a redundant
        # SSH stat command on remote hosts (is_lock_held already checked existence).
        locked_time = host.get_reported_lock_time() if is_locked else None
    else:
        boot_time = None
        uptime_seconds = None
        resource = None

    # make the host data
    certified_data = host.get_certified_data()
    host_plugin_data = certified_data.plugin
    # Always use the certified host_name for consistency between online and offline hosts.
    # Online hosts would otherwise return the SSH hostname (e.g., "r438.modal.host") via
    # get_name(), while offline hosts return the friendly name from certified data.
    host_name = certified_data.host_name
    # SSH activity is tracked at the host level (host_dir/activity/ssh)
    ssh_activity = (
        host.get_reported_activity_time(ActivitySource.SSH) if isinstance(host, OnlineHostInterface) else None
    )
    host_info = HostInfo(
        id=host.id,
        name=host_name,
        provider_name=host_ref.provider_name,
        state=host.get_state() if not is_authentication_failure else HostState.UNAUTHENTICATED,
        image=host.get_image(),
        tags=host.get_tags(),
        boot_time=boot_time,
        uptime_seconds=uptime_seconds,
        resource=resource,
        ssh=ssh_info,
        snapshots=host.get_snapshots(),
        is_locked=is_locked,
        locked_time=locked_time,
        plugin=host_plugin_data,
        ssh_activity_time=ssh_activity,
        failure_reason=host.get_failure_reason(),
    )

    # Get all agents on this host
    agents = None
    if isinstance(host, OnlineHostInterface):
        agents = host.get_agents()

    # make an AgentInfo for each agent on this host
    for agent_ref in agent_refs:
        try:
            agent_info: AgentInfo | None = None
            if agents is not None:
                # Find the agent in the list for running hosts
                agent = next((a for a in (agents or []) if a.id == agent_ref.agent_id), None)

                if agent is None:
                    exception = AgentNotFoundOnHostError(agent_ref.agent_id, host_ref.host_id)
                    if params.error_behavior == ErrorBehavior.ABORT:
                        raise exception
                    error_info = AgentErrorInfo.build_for_agent(exception, agent_ref.agent_id)
                    with results_lock:
                        result.errors.append(error_info)
                    if params.on_error:
                        params.on_error(error_info)
                    continue

                # Get activity config from host
                activity_config = host.get_activity_config()

                # Activity times from file mtimes (per-agent)
                user_activity = agent.get_reported_activity_time(ActivitySource.USER)
                agent_activity = agent.get_reported_activity_time(ActivitySource.AGENT)

                # start_time from activity/start file mtime (not the status/start_time file)
                start_time = agent.get_reported_activity_time(ActivitySource.START)

                # runtime_seconds computed from start_time
                now = datetime.now(timezone.utc)
                runtime_seconds = (now - start_time).total_seconds() if start_time else None

                # idle_seconds: include host-level ssh_activity; 0.0 if no activity yet
                idle_seconds = compute_idle_seconds(user_activity, agent_activity, ssh_activity) or 0.0

                agent_info = AgentInfo(
                    id=agent.id,
                    name=agent.name,
                    type=str(agent.agent_type),
                    command=agent.get_command(),
                    work_dir=agent.work_dir,
                    create_time=agent.create_time,
                    start_on_boot=agent.get_is_start_on_boot(),
                    state=agent.get_lifecycle_state(),
                    url=agent.get_reported_url(),
                    start_time=start_time,
                    runtime_seconds=runtime_seconds,
                    user_activity_time=user_activity,
                    agent_activity_time=agent_activity,
                    idle_seconds=idle_seconds,
                    idle_mode=activity_config.idle_mode.value,
                    idle_timeout_seconds=activity_config.idle_timeout_seconds,
                    activity_sources=tuple(s.value for s in activity_config.activity_sources),
                    labels=agent.get_labels(),
                    host=host_info,
                    plugin={},
                )
            # if this host is offline, or if we failed to get the online host (ex: because it went offline)
            if agents is None or agent_info is None:
                # Use certified_data from the agent_ref directly
                # agent_ref already has all the data we need from host.get_agent_references()
                create_time = agent_ref.create_time or datetime(1970, 1, 1, tzinfo=timezone.utc)
                agent_info = AgentInfo(
                    id=agent_ref.agent_id,
                    name=agent_ref.agent_name,
                    type=str(agent_ref.agent_type) if agent_ref.agent_type else "unknown",
                    command=agent_ref.command or CommandString(""),
                    work_dir=agent_ref.work_dir or Path("/"),
                    create_time=create_time,
                    start_on_boot=agent_ref.start_on_boot,
                    state=AgentLifecycleState.STOPPED,
                    url=None,
                    start_time=None,
                    runtime_seconds=None,
                    user_activity_time=None,
                    agent_activity_time=None,
                    idle_seconds=None,
                    idle_mode=None,
                    labels=agent_ref.labels,
                    host=host_info,
                    plugin={},
                )

            # Apply CEL filters if provided
            if params.compiled_include_filters or params.compiled_exclude_filters:
                if not _apply_cel_filters(
                    agent_info, params.compiled_include_filters, params.compiled_exclude_filters
                ):
                    continue

            with results_lock:
                result.agents.append(agent_info)
            if params.on_agent:
                params.on_agent(agent_info)

        except MngError as e:
            if params.error_behavior == ErrorBehavior.ABORT:
                raise
            error_info = AgentErrorInfo.build_for_agent(e, agent_ref.agent_id)
            with results_lock:
                result.errors.append(error_info)
            if params.on_error:
                params.on_error(error_info)


def _process_host_for_agent_listing(
    host_ref: HostReference,
    agent_refs: list[AgentReference],
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
        _assemble_host_info(
            host_ref,
            agent_refs,
            provider,
            params,
            result,
            results_lock,
        )

    except MngError as e:
        if params.error_behavior == ErrorBehavior.ABORT:
            raise
        error_info = HostErrorInfo.build_for_host(e, host_ref.host_id)
        with results_lock:
            result.errors.append(error_info)
        if params.on_error:
            params.on_error(error_info)


@pure
def _agent_to_cel_context(agent: AgentInfo) -> dict[str, Any]:
    """Convert an AgentInfo object to a CEL-friendly dict.

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
    agent: AgentInfo,
    include_filters: Sequence[Any],
    exclude_filters: Sequence[Any],
) -> bool:
    """Apply CEL filters to an agent.

    Returns True if the agent should be included (matches all include filters
    and doesn't match any exclude filters).
    """
    context = _agent_to_cel_context(agent)
    return apply_cel_filters_to_context(
        context=context,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        error_context_description=f"agent {agent.name}",
    )


def _warn_on_duplicate_host_names(
    agents_by_host: dict[HostReference, list[AgentReference]],
) -> None:
    """Emit a warning if any host names are duplicated within the same provider.

    This should never happen in normal operation -- it indicates a bug or race condition
    in host creation.

    Only considers hosts that have at least one agent reference, since destroyed
    hosts (which typically have no agents) may legitimately share a name with a
    newly created host.
    """
    # Group host names by provider, tracking which host IDs share each name
    host_ids_by_provider_and_name: dict[tuple[ProviderInstanceName, HostName], list[HostId]] = {}
    for host_ref, agent_refs in agents_by_host.items():
        if not agent_refs:
            continue
        key = (host_ref.provider_name, host_ref.host_name)
        host_ids_by_provider_and_name.setdefault(key, []).append(host_ref.host_id)

    for (provider_name, host_name), host_ids in host_ids_by_provider_and_name.items():
        if len(host_ids) > 1:
            logger.warning(
                "Duplicate host name '{}' found on provider '{}' (host IDs: {}). "
                "This should never happen -- it may indicate a bug or a race condition during host creation.",
                host_name,
                provider_name,
                ", ".join(str(hid) for hid in host_ids),
            )


def _process_provider_for_host_listing(
    provider: BaseProviderInstance,
    agents_by_host: dict[HostReference, list[AgentReference]],
    include_destroyed: bool,
    results_lock: Lock,
    cg: ConcurrencyGroup,
) -> None:
    """Process a single provider and collect its hosts and agents.

    This function is run in a thread by load_all_agents_grouped_by_host.
    Results are merged into the shared agents_by_host dict under the results_lock.
    """
    provider_results = provider.load_agent_refs(cg=cg, include_destroyed=include_destroyed)

    # Merge results into the main dict under lock
    with results_lock:
        agents_by_host.update(provider_results)


@log_call
def load_all_agents_grouped_by_host(
    mng_ctx: MngContext, provider_names: tuple[str, ...] | None = None, include_destroyed: bool = False
) -> tuple[dict[HostReference, list[AgentReference]], list[BaseProviderInstance]]:
    """Load all agents from all providers, grouped by their host.

    Uses ConcurrencyGroup to query providers in parallel for better performance.
    Handles both online hosts (which can be queried directly) and offline hosts (which use persisted data).
    """
    agents_by_host: dict[HostReference, list[AgentReference]] = {}
    results_lock = Lock()

    with log_span("Loading all agents from all providers"):
        providers = get_all_provider_instances(mng_ctx, provider_names)
        logger.trace("Found {} provider instances", len(providers))

        # Process all providers in parallel using ConcurrencyGroupExecutor
        futures: list[Future[None]] = []
        with ConcurrencyGroupExecutor(
            parent_cg=mng_ctx.concurrency_group, name="load_all_agents_grouped_by_host", max_workers=32
        ) as executor:
            for provider in providers:
                futures.append(
                    executor.submit(
                        _process_provider_for_host_listing,
                        provider,
                        agents_by_host,
                        include_destroyed,
                        results_lock,
                        mng_ctx.concurrency_group,
                    )
                )

        # Re-raise any thread exceptions
        for future in futures:
            future.result()

        # Warn if any host names are duplicated within the same provider
        _warn_on_duplicate_host_names(agents_by_host)

        return (agents_by_host, providers)
