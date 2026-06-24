import typing
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from threading import Lock
from typing import Any
from typing import Final

from loguru import logger
from pydantic import BaseModel
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import warn_on_duplicate_host_names
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import emit_discovery_error_event
from imbue.mngr.api.discovery_events import emit_host_ssh_info
from imbue.mngr.api.discovery_events import extract_agents_and_hosts_from_full_listing
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.api.discovery_events import write_full_discovery_snapshot
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.providers import list_provider_names_to_load
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderDiscoveryError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderError
from imbue.mngr.errors import ProviderInstanceNotFoundError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.cel_utils import apply_compiled_cel_filters
from imbue.mngr.utils.cel_utils import build_cel_context
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.cel_utils import with_tolerant_paths
from imbue.mngr.utils.pydantic_utils import unwrap_optional
from imbue.mngr.utils.thread_cleanup import mngr_executor


def _walk_dict_paths(model: type[BaseModel], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Yield every path through `model`'s pydantic field tree that terminates in a dict-typed field.

    Recurses into nested model fields, unwraps `Optional[T]` to T, and treats
    a `dict[...]` / `Mapping[...]` field as a leaf. Anything else (lists,
    primitives, datetimes, etc.) is not a path target.

    Used to compute `_AGENT_SCHEMALESS_PATHS` from the AgentDetails type tree
    at module load time -- see the rationale on that constant.

    This is intentionally separate from `utils.model_schema.walk_model_fields`
    (which enumerates *all* fields for the `--schema` view): this walk yields
    only dict-typed leaf paths as tuples and has no use for non-dict leaves, so
    expressing it on the general walker would be more indirect, not less.
    """
    paths: list[tuple[str, ...]] = []
    for name, field in model.model_fields.items():
        annotation = unwrap_optional(field.annotation)
        if _is_dict_like(annotation):
            paths.append((*prefix, name))
            continue
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            paths.extend(_walk_dict_paths(annotation, (*prefix, name)))
            continue
        # Anything else (primitives, lists, tuples, datetimes, enums, ...) is
        # not a dict and not a model we can descend into, so we skip it.
    return paths


def _is_dict_like(annotation: Any) -> bool:
    """True if `annotation` is `dict[...]` or `Mapping[...]` (or a subclass origin)."""
    origin = typing.get_origin(annotation)
    if origin is None:
        return False
    return origin is dict or (isinstance(origin, type) and issubclass(origin, Mapping))


# CEL paths whose missing-key access should evaluate to a clean False (and let
# `has()` report absence) rather than warn per agent. Derived at module load
# time by walking the AgentDetails pydantic field tree: every `dict[...]`-typed
# field is treated as schemaless because its contents are user- or
# plugin-supplied, so different agents legitimately have different keys.
#
# Adding a new dict field to AgentDetails or HostDetails automatically opts it
# into tolerance. If a future dict field is actually *schemaful* (typos should
# warn), we'll need a marker to opt it out -- flag it at the time, none today.
# See `with_tolerant_paths` in cel_utils for the tolerance semantics.
_AGENT_SCHEMALESS_PATHS: Final[tuple[tuple[str, ...], ...]] = tuple(_walk_dict_paths(AgentDetails))


class ErrorInfo(FrozenModel):
    """Information about an error encountered during listing.

    This preserves the exception type and message instead of converting to a string immediately.
    """

    exception_type: str = Field(description="The type name of the exception (e.g., 'RuntimeError')")
    message: str = Field(description="The error message")
    # True when the underlying exception is a ProviderUnavailableError (which now
    # includes ProviderNotAuthorizedError). Lets the CLI pick the granular
    # provider-inaccessible exit code without re-parsing the message or type name.
    is_provider_inaccessible: bool = Field(
        default=False,
        description="Whether this error means a provider was unreachable or unauthenticated",
    )
    # Verbose, multi-line remediation guidance (from MngrError.user_help_text), if any.
    help_text: str | None = Field(default=None, description="Verbose remediation guidance for the user")

    @classmethod
    def build(cls, exception: BaseException) -> "ErrorInfo":
        """Build an ErrorInfo from an exception."""
        return cls(
            exception_type=type(exception).__name__,
            message=str(exception),
            is_provider_inaccessible=isinstance(exception, ProviderUnavailableError),
            help_text=exception.user_help_text if isinstance(exception, MngrError) else None,
        )


class ProviderErrorInfo(ErrorInfo):
    """Error information with provider context."""

    provider_name: ProviderInstanceName = Field(description="Name of the provider where the error occurred")
    # Concise reason/remediation lifted from ProviderUnavailableError so callers can
    # render a consistent one-line summary; None for non-provider-unavailable failures.
    short_reason: str | None = Field(default=None, description="Concise reason the provider is unavailable")
    short_remediation: str | None = Field(default=None, description="Concise next step the user can take")

    @classmethod
    def build_for_provider(cls, exception: BaseException, provider_name: ProviderInstanceName) -> "ProviderErrorInfo":
        """Build a ProviderErrorInfo from an exception and provider name."""
        return cls(
            exception_type=type(exception).__name__,
            message=str(exception),
            provider_name=provider_name,
            is_provider_inaccessible=isinstance(exception, ProviderUnavailableError),
            help_text=exception.user_help_text if isinstance(exception, MngrError) else None,
            short_reason=exception.short_reason if isinstance(exception, ProviderUnavailableError) else None,
            short_remediation=exception.short_remediation if isinstance(exception, ProviderUnavailableError) else None,
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
    offline_field_generators: dict[str, dict[str, Callable[[DiscoveredAgent, HostDetails], Any]]] = Field(
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
    # If specified, only list agents from these providers
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

        offline_field_generators: dict[str, dict[str, Callable[[DiscoveredAgent, HostDetails], Any]]] = {}
        for offline_hook_result in mngr_ctx.pm.hook.offline_agent_field_generators():
            if offline_hook_result is not None:
                offline_plugin_name, offline_generators = offline_hook_result
                offline_field_generators[offline_plugin_name] = offline_generators

        params = _ListAgentsParams(
            compiled_include_filters=compiled_include_filters,
            compiled_exclude_filters=compiled_exclude_filters,
            error_behavior=error_behavior,
            on_agent=on_agent,
            on_error=on_error,
            field_generators=field_generators,
            offline_field_generators=offline_field_generators,
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

    A snapshot is written whenever the listing represents the full state:
    - All providers were queried (no provider_names filter)
    - No CEL filters were applied (the result contains every agent)

    Per-provider discovery errors are NOT a reason to skip emission: the
    snapshot is authoritative state, and consumers need to see which
    providers succeeded vs. failed in order to render reality. The `providers`
    and `error_by_provider_name` fields carry that information.
    """
    is_full_listing = provider_names is None and not include_filters and not exclude_filters
    if not is_full_listing:
        return
    # Skip if any error is something other than a per-provider error. This
    # filter currently lumps three classes together: plain `ErrorInfo` from the
    # top-level `except MngrError` (truly non-attributable), plus `HostErrorInfo`
    # and `AgentErrorInfo` (attributable to a host/agent but not modeled in the
    # snapshot today). In all three cases the result may be structurally
    # incomplete in ways the snapshot's `error_by_provider_name` field cannot
    # represent, so we skip emission rather than mislead consumers. Only
    # per-provider failures are handled in-band via `error_by_provider_name`;
    # surfacing per-host / per-agent errors in the snapshot is out of scope for
    # this change.
    non_provider_errors = [e for e in result.errors if not isinstance(e, ProviderErrorInfo)]
    if non_provider_errors:
        logger.trace(
            "Skipping full discovery snapshot: {} non-provider-attributable error(s) during listing",
            len(non_provider_errors),
        )
        return
    try:
        discovered_agents, discovered_hosts, host_ssh_infos = extract_agents_and_hosts_from_full_listing(result.agents)
        snapshot_providers, error_by_provider_name = _build_provider_snapshot_state(mngr_ctx, result)
        write_full_discovery_snapshot(
            mngr_ctx.config,
            discovered_agents,
            discovered_hosts,
            providers=snapshot_providers,
            error_by_provider_name=error_by_provider_name,
        )
        for host_id, ssh_info in host_ssh_infos:
            emit_host_ssh_info(mngr_ctx.config, host_id, ssh_info)
    except (MngrError, OSError) as e:
        logger.warning("Failed to write full discovery snapshot: {}", e)


def _build_provider_snapshot_state(
    mngr_ctx: MngrContext,
    result: ListResult,
) -> tuple[tuple[DiscoveredProvider, ...], dict[ProviderInstanceName, DiscoveryError]]:
    """Derive the snapshot's providers and error_by_provider_name fields from listing data.

    A provider lands in `error_by_provider_name` if it has any ``ProviderErrorInfo`` on
    ``result.errors`` (whether the failure was in construction or in discovery; both
    paths use the same error type). Every other provider that we attempted to load
    in this listing lands in `providers` -- including ones that successfully reported
    zero hosts/agents (e.g. ProviderEmptyError, which is silently skipped).
    """
    error_by_provider_name: dict[ProviderInstanceName, DiscoveryError] = {}
    for error_info in result.errors:
        if isinstance(error_info, ProviderErrorInfo):
            error_by_provider_name[error_info.provider_name] = DiscoveryError(
                type_name=error_info.exception_type,
                message=error_info.message,
                provider_name=error_info.provider_name,
            )

    candidate_names = list_provider_names_to_load(mngr_ctx)
    snapshot_providers: list[DiscoveredProvider] = []
    for name in candidate_names:
        if name in error_by_provider_name:
            continue
        config = _get_provider_config_for_snapshot(name, mngr_ctx)
        snapshot_providers.append(make_discovered_provider(name, config))
    return tuple(snapshot_providers), error_by_provider_name


def _get_provider_config_for_snapshot(
    name: ProviderInstanceName,
    mngr_ctx: MngrContext,
) -> ProviderInstanceConfig:
    """Return the config block for `name`, or a base default for backend-default instances.

    Providers named explicitly in `mngr_ctx.config.providers` use their configured
    block; backends that are loaded via the implicit-default path (no explicit
    `[providers.<backend>]` block) get a minimal placeholder so the snapshot can
    still surface them.
    """
    explicit = mngr_ctx.config.providers.get(name)
    if explicit is not None:
        return explicit
    return ProviderInstanceConfig(backend=ProviderBackendName(str(name)))


def _construct_and_discover_for_provider(
    provider_name: ProviderInstanceName,
    mngr_ctx: MngrContext,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
    reset_caches: bool,
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    providers: list[ProviderInstanceInterface],
    providers_lock: Lock,
) -> None:
    """Construct one provider and discover its hosts/agents, merging into shared dicts.

    On failure, honors `params.error_behavior`: ABORT re-raises (wrapped to
    `MngrError`); CONTINUE records a `ProviderErrorInfo` on `result.errors`.
    """
    try:
        provider = get_provider_instance(provider_name, mngr_ctx)
        if reset_caches:
            provider.reset_caches()
        provider_results = provider.discover_hosts_and_agents(cg=mngr_ctx.concurrency_group, include_destroyed=True)
    except ProviderEmptyError as e:
        # Provider was reached and is known-empty (e.g. Modal env not yet
        # created). Always safe to silently skip in listing -- there is
        # provably nothing to enumerate, so the resulting listing stays
        # correct rather than misleading. Distinct from
        # ``ProviderUnavailableError`` whose state is unknown.
        logger.debug("Skipping provider {} (empty -- nothing to list): {}", provider_name, e)
        return
    except Exception as e:
        if params.error_behavior == ErrorBehavior.ABORT:
            # A ProviderError already carries provider_name and renders a clean,
            # attributable message (e.g. ProviderUnavailableError /
            # ProviderNotAuthorizedError), so re-raise it as-is -- this keeps the
            # auth/unavailable message consistent and lets the CLI map it to the
            # provider-inaccessible exit code. Only wrap genuinely
            # non-attributable failures so downstream handlers (e.g.
            # discovery_events' _write_unfiltered_full_snapshot_logged, minds'
            # providers panel) can still recover a provider_name.
            if isinstance(e, ProviderError):
                raise
            raise ProviderDiscoveryError(provider_name, e) from e
        # Expected, handled conditions (provider unreachable / unauthenticated) are
        # surfaced cleanly via result.errors, so log them at debug to avoid noisy
        # error-level duplicates; only unexpected failures get an error + traceback.
        if isinstance(e, ProviderUnavailableError):
            logger.debug("Provider {} is unavailable: {}", provider_name, e)
        else:
            logger.opt(exception=e).error("Error discovering agents for provider {}", provider_name)
        emit_discovery_error_event(
            mngr_ctx.config,
            error_type=type(e).__name__,
            error_message=str(e),
            source_name=str(provider_name),
            provider_name=str(provider_name),
        )
        error_info = ProviderErrorInfo.build_for_provider(e, provider_name)
        with results_lock:
            result.errors.append(error_info)
        if params.on_error:
            params.on_error(error_info)
        return

    with providers_lock:
        providers.append(provider)
    with results_lock:
        agents_by_host.update(provider_results)


def _construct_and_discover_all_providers(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
    reset_caches: bool,
) -> tuple[dict[DiscoveredHost, list[DiscoveredAgent]], list[ProviderInstanceInterface]]:
    """Run `_construct_and_discover_for_provider` for every provider in parallel.

    Returns the merged host/agent map plus the providers that completed
    successfully.
    """
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
    providers: list[ProviderInstanceInterface] = []
    providers_lock = Lock()

    with log_span("Loading agents from all providers"):
        names = list_provider_names_to_load(mngr_ctx, provider_names)
        with mngr_executor(
            parent_cg=mngr_ctx.concurrency_group, name="list_agents_construct_and_discover", max_workers=32
        ) as executor:
            futures = [
                executor.submit(
                    _construct_and_discover_for_provider,
                    name,
                    mngr_ctx,
                    params,
                    result,
                    results_lock,
                    reset_caches,
                    agents_by_host,
                    providers,
                    providers_lock,
                )
                for name in names
            ]

        # Re-raise any thread exceptions (ABORT-mode errors)
        for future in futures:
            future.result()

    warn_on_duplicate_host_names(agents_by_host)
    return agents_by_host, providers


def _list_agents_batch(
    mngr_ctx: MngrContext,
    provider_names: tuple[str, ...] | None,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
    reset_caches: bool = False,
) -> None:
    """Batch mode: load all agents from all providers, then process hosts."""
    agents_by_host, providers = _construct_and_discover_all_providers(
        mngr_ctx=mngr_ctx,
        provider_names=provider_names,
        params=params,
        result=result,
        results_lock=results_lock,
        reset_caches=reset_caches,
    )
    provider_map = {provider.name: provider for provider in providers}
    logger.trace("Found {} hosts with agents", len(agents_by_host))

    # Process each host and its agents in parallel
    futures: list[Future[None]] = []
    with mngr_executor(
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
        names = list_provider_names_to_load(mngr_ctx, provider_names)
        logger.trace("Found {} provider names to load", len(names))

        with mngr_executor(
            parent_cg=mngr_ctx.concurrency_group, name="list_agents_streaming", max_workers=32
        ) as executor:
            streaming_futures: list[Future[None]] = []
            for name in names:
                streaming_futures.append(
                    executor.submit(
                        _construct_discover_and_emit_for_provider,
                        name,
                        mngr_ctx,
                        params,
                        result,
                        results_lock,
                        reset_caches,
                    )
                )

        # Re-raise any thread exceptions
        for future in streaming_futures:
            future.result()


def _construct_discover_and_emit_for_provider(
    provider_name: ProviderInstanceName,
    mngr_ctx: MngrContext,
    params: _ListAgentsParams,
    result: ListResult,
    results_lock: Lock,
    reset_caches: bool,
) -> None:
    """Construct a single provider, load its hosts, and process them.

    Streaming counterpart to the batch approach. Each provider independently
    constructs, loads hosts, fetches agent references, then processes hosts --
    firing on_agent callbacks without waiting for other providers.
    """
    cg = mngr_ctx.concurrency_group
    try:
        provider = get_provider_instance(provider_name, mngr_ctx)
        if reset_caches:
            provider.reset_caches()

        # Phase 1: list hosts and get agent refs
        provider_results = provider.discover_hosts_and_agents(cg=cg, include_destroyed=True)

        # Warn if any host names are duplicated within this provider
        warn_on_duplicate_host_names(provider_results)

        # Phase 2: immediately process hosts (fire on_agent for this provider)
        host_futures: list[Future[None]] = []
        with mngr_executor(parent_cg=cg, name=f"stream_hosts_{provider.name}", max_workers=32) as executor:
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

    except ProviderEmptyError as e:
        # See _construct_and_discover_for_provider's matching arm: known-empty
        # provider is always safe to skip in listing.
        logger.debug("Skipping provider {} (empty -- nothing to list): {}", provider_name, e)
    except Exception as e:
        if params.error_behavior == ErrorBehavior.ABORT:
            if isinstance(e, MngrError):
                raise
            raise MngrError(str(e)) from e
        # Expected, handled conditions (provider unreachable / unauthenticated) are
        # surfaced cleanly via result.errors, so log them at debug to avoid noisy
        # error-level duplicates; only unexpected failures get an error + traceback.
        if isinstance(e, ProviderUnavailableError):
            logger.debug("Provider {} is unavailable: {}", provider_name, e)
        else:
            logger.opt(exception=e).error("Error discovering agents for provider {}", provider_name)
        emit_discovery_error_event(
            mngr_ctx.config,
            error_type=type(e).__name__,
            error_message=str(e),
            source_name=str(provider_name),
            provider_name=str(provider_name),
        )
        error_info = ProviderErrorInfo.build_for_provider(e, provider_name)
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
        field_generators=params.field_generators,
        offline_field_generators=params.offline_field_generators,
        on_error=lambda source, exc: _handle_listing_error(source, exc, params, result, results_lock),
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

    except Exception as e:
        if params.error_behavior == ErrorBehavior.ABORT:
            if isinstance(e, MngrError):
                raise
            raise MngrError(str(e)) from e
        logger.opt(exception=e).error("Error processing host {}", host_ref.host_id)
        emit_discovery_error_event(
            provider.mngr_ctx.config,
            error_type=type(e).__name__,
            error_message=str(e),
            source_name=str(host_ref.host_id),
            provider_name=str(provider.name),
        )
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

    # Add idle: seconds since the most recent activity across user, agent, and host SSH.
    host_dict = result["host"] if isinstance(result.get("host"), dict) else None
    activity_candidates = [
        result.get("user_activity_time"),
        result.get("agent_activity_time"),
        host_dict.get("ssh_activity_time") if host_dict else None,
    ]
    latest_activity = None
    for activity_time in activity_candidates:
        if not activity_time:
            continue
        if isinstance(activity_time, str):
            activity_dt = datetime.fromisoformat(activity_time.replace("Z", "+00:00"))
        else:
            activity_dt = activity_time
        if latest_activity is None or activity_dt > latest_activity:
            latest_activity = activity_dt
    if latest_activity:
        result["idle"] = (datetime.now(timezone.utc) - latest_activity).total_seconds()

    # Expose host.provider_name as host.provider too, so CEL filters can use either name
    # (host.provider is the documented short form; host.provider_name matches the data type)
    if host_dict is not None and "provider_name" in host_dict:
        host_dict["provider"] = host_dict["provider_name"]

    # Expose labels.project as the bare `project` alias too, mirroring the --project
    # filter flag and the host.provider alias, so CEL filters/sorts can use either name.
    # Always set (None when unset), matching how optional scalar fields appear in the dump.
    labels = result.get("labels")
    result["project"] = labels.get("project") if isinstance(labels, dict) else None

    return result


def build_agent_cel_context(agent: AgentDetails) -> dict[str, Any]:
    """Build a CEL evaluation context for `agent` with schemaless fields wrapped tolerantly.

    Composes the three steps (`agent_details_to_cel_context` ->
    `build_cel_context` -> `with_tolerant_paths`) so that filter and sort
    callers see a consistent view: missing keys under the schemaless fields
    listed in `_AGENT_SCHEMALESS_PATHS` evaluate to a clean False instead of
    raising at evaluation time.
    """
    return with_tolerant_paths(
        build_cel_context(agent_details_to_cel_context(agent)),
        _AGENT_SCHEMALESS_PATHS,
    )


def _apply_cel_filters(
    agent: AgentDetails,
    include_filters: Sequence[Any],
    exclude_filters: Sequence[Any],
) -> bool:
    """Apply CEL filters to an agent.

    Returns True if the agent should be included (matches all include filters
    and doesn't match any exclude filters).
    """
    return apply_compiled_cel_filters(
        cel_context=build_agent_cel_context(agent),
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        error_context_description=f"agent {agent.name}",
    )
