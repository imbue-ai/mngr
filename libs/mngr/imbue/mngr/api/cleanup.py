from typing import assert_never

from loguru import logger

from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.mngr.api.data_types import CleanupResult
from imbue.mngr.api.data_types import GcResourceTypes
from imbue.mngr.api.discovery_events import emit_agent_destroyed
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.discovery_events import emit_host_destroyed
from imbue.mngr.api.gc import gc as api_gc
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import LocalHostNotDestroyableError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderInstanceNotFoundError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CleanupAction
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId

# Exception types that mean cleanup could not even be attempted (the host/provider
# was unreachable or the host is intentionally not destroyable / unsupported).
_PROVIDER_INACCESSIBLE_EXCEPTIONS = (
    HostNotFoundError,
    ProviderInstanceNotFoundError,
    ProviderUnavailableError,
    LocalHostNotDestroyableError,
    NotImplementedError,
)


def _category_for_destroy_host_error(error: Exception) -> CleanupFailureCategory:
    """Classify an exception raised out of ``provider.destroy_host``.

    A "could not even attempt" error (host unreachable / not destroyable) is
    PROVIDER_INACCESSIBLE; anything else is OTHER.
    """
    if isinstance(error, _PROVIDER_INACCESSIBLE_EXCEPTIONS):
        return CleanupFailureCategory.PROVIDER_INACCESSIBLE
    return CleanupFailureCategory.OTHER


@log_call
def find_agents_for_cleanup(
    mngr_ctx: MngrContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    error_behavior: ErrorBehavior,
) -> list[AgentDetails]:
    """Find agents matching the given filters for cleanup."""
    result = list_agents(
        mngr_ctx=mngr_ctx,
        is_streaming=False,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        error_behavior=error_behavior,
    )
    return result.agents


@log_call
def execute_cleanup(
    mngr_ctx: MngrContext,
    agents: list[AgentDetails],
    action: CleanupAction,
    is_dry_run: bool,
    error_behavior: ErrorBehavior,
) -> CleanupResult:
    """Execute the cleanup action (destroy or stop) on the given agents."""
    result = CleanupResult()

    if is_dry_run:
        match action:
            case CleanupAction.DESTROY:
                result.destroyed_agents = [agent.name for agent in agents]
            case CleanupAction.STOP:
                result.stopped_agents = [agent.name for agent in agents]
            case _ as unreachable:
                assert_never(unreachable)
        return result

    # Group agents by host
    agents_by_host: dict[HostId, list[AgentDetails]] = {}
    for agent in agents:
        host_id = agent.host.id
        if host_id not in agents_by_host:
            agents_by_host[host_id] = []
        agents_by_host[host_id].append(agent)

    match action:
        case CleanupAction.DESTROY:
            _execute_destroy(mngr_ctx, agents_by_host, result, error_behavior)
        case CleanupAction.STOP:
            _execute_stop(mngr_ctx, agents_by_host, result, error_behavior)
        case _ as unreachable:
            assert_never(unreachable)

    # Run post-destroy GC when destroying
    if action == CleanupAction.DESTROY and result.destroyed_agents:
        _run_post_cleanup_gc(mngr_ctx, result)

    return result


def _execute_destroy(
    mngr_ctx: MngrContext,
    agents_by_host: dict[HostId, list[AgentDetails]],
    result: CleanupResult,
    error_behavior: ErrorBehavior,
) -> None:
    """Destroy agents, grouped by host."""
    for host_id, host_agents in agents_by_host.items():
        provider_name = host_agents[0].host.provider_name
        try:
            provider = get_provider_instance(provider_name, mngr_ctx)
            host = provider.get_host(host_id)
        except MngrError as e:
            # Could not access the host at all -- nothing was cleaned up.
            error_msg = f"Error accessing host {host_id}: {e}"
            logger.warning(error_msg)
            result.failures.append(
                CleanupFailure(
                    category=CleanupFailureCategory.PROVIDER_INACCESSIBLE, message=error_msg, host_id=host_id
                )
            )
            if error_behavior == ErrorBehavior.ABORT:
                return
            continue

        match host:
            case OnlineHostInterface() as online_host:
                with log_span("Destroying agents on online host {}", host_id):
                    for agent_details in host_agents:
                        try:
                            # Find the agent interface on the host
                            for agent in online_host.get_agents():
                                if agent.id == agent_details.id:
                                    mngr_ctx.pm.hook.on_before_agent_destroy(agent=agent, host=online_host)
                                    # destroy_agent is best-effort: it raises a CleanupFailedGroup
                                    # carrying the real failures (resources left behind) rather than
                                    # failing fast. We still acted, so record the agent regardless.
                                    try:
                                        online_host.destroy_agent(agent)
                                    except CleanupFailedGroup as group:
                                        result.failures.extend(group.failures)
                                    mngr_ctx.pm.hook.on_agent_destroyed(agent=agent, host=online_host)
                                    result.destroyed_agents.append(agent_details.name)
                                    logger.debug("Destroyed agent: {}", agent_details.name)
                                    emit_agent_destroyed(mngr_ctx.config, agent_details.id, host_id)
                                    emit_discovery_events_for_host(mngr_ctx.config, online_host)
                                    break
                            else:
                                # Agent not found on host (already gone) -- benign.
                                logger.debug(
                                    "Agent {} not found on host, treating as already destroyed",
                                    agent_details.name,
                                )
                                result.destroyed_agents.append(agent_details.name)
                        except MngrError as e:
                            # A hook or host-access error while destroying this agent.
                            error_msg = f"Error destroying agent {agent_details.name}: {e}"
                            logger.warning(error_msg)
                            result.failures.append(
                                CleanupFailure(
                                    category=CleanupFailureCategory.OTHER,
                                    message=error_msg,
                                    agent_name=agent_details.name,
                                    host_id=host_id,
                                )
                            )
                            if error_behavior == ErrorBehavior.ABORT:
                                return
            case HostInterface() as offline_host:
                with log_span("Destroying offline host {} with {} agent(s)", host_id, len(host_agents)):
                    try:
                        mngr_ctx.pm.hook.on_before_host_destroy(host=offline_host, mngr_ctx=mngr_ctx)
                        # destroy_host is best-effort: it raises a CleanupFailedGroup carrying the
                        # real failures (resources left behind) rather than failing fast.
                        try:
                            provider.destroy_host(offline_host)
                        except CleanupFailedGroup as group:
                            result.failures.extend(group.failures)
                        mngr_ctx.pm.hook.on_host_destroyed(host=offline_host, mngr_ctx=mngr_ctx)
                    except (MngrError, NotImplementedError) as e:
                        # Could not destroy the host at all.
                        error_msg = f"Error destroying offline host {host_id}: {e}"
                        logger.warning(error_msg)
                        result.failures.append(
                            CleanupFailure(
                                category=_category_for_destroy_host_error(e), message=error_msg, host_id=host_id
                            )
                        )
                        if error_behavior == ErrorBehavior.ABORT:
                            return
                    else:
                        for agent_details in host_agents:
                            result.destroyed_agents.append(agent_details.name)
                            logger.debug("Destroyed agent: {} (via host destruction)", agent_details.name)
                        emit_host_destroyed(mngr_ctx.config, host_id, [ad.id for ad in host_agents])
            case _ as unreachable:
                assert_never(unreachable)


def _execute_stop(
    mngr_ctx: MngrContext,
    agents_by_host: dict[HostId, list[AgentDetails]],
    result: CleanupResult,
    error_behavior: ErrorBehavior,
) -> None:
    """Stop agents, grouped by host."""
    for host_id, host_agents in agents_by_host.items():
        provider_name = host_agents[0].host.provider_name
        try:
            provider = get_provider_instance(provider_name, mngr_ctx)
            host = provider.get_host(host_id)
        except MngrError as e:
            error_msg = f"Error accessing host {host_id}: {e}"
            logger.warning(error_msg)
            result.failures.append(
                CleanupFailure(
                    category=CleanupFailureCategory.PROVIDER_INACCESSIBLE, message=error_msg, host_id=host_id
                )
            )
            if error_behavior == ErrorBehavior.ABORT:
                return
            continue

        match host:
            case OnlineHostInterface() as online_host:
                with log_span("Stopping agents on host {}", host_id):
                    agent_ids_to_stop = [agent_details.id for agent_details in host_agents]
                    try:
                        # stop_agents is best-effort: it raises a CleanupFailedGroup carrying the
                        # real failures (resources left behind) rather than failing fast. We still
                        # acted on these agents, so record them stopped regardless.
                        try:
                            online_host.stop_agents(agent_ids_to_stop)
                        except CleanupFailedGroup as group:
                            result.failures.extend(group.failures)
                        for agent_details in host_agents:
                            result.stopped_agents.append(agent_details.name)
                            logger.debug("Stopped agent: {}", agent_details.name)
                    except MngrError as e:
                        error_msg = f"Error stopping agents on host {host_id}: {e}"
                        logger.warning(error_msg)
                        result.failures.append(
                            CleanupFailure(category=CleanupFailureCategory.OTHER, message=error_msg, host_id=host_id)
                        )
                        if error_behavior == ErrorBehavior.ABORT:
                            return
            case HostInterface():
                # The host is offline, so we cannot reach it to stop (or verify the state of)
                # its agents. We do not know whether they are still running, so this is a real
                # PROVIDER_INACCESSIBLE failure rather than a benign no-op.
                error_msg = f"Cannot stop {len(host_agents)} agent(s) on offline host {host_id} (host is unreachable)"
                logger.warning(error_msg)
                result.failures.append(
                    CleanupFailure(
                        category=CleanupFailureCategory.PROVIDER_INACCESSIBLE, message=error_msg, host_id=host_id
                    )
                )
                if error_behavior == ErrorBehavior.ABORT:
                    return
            case _ as unreachable:
                assert_never(unreachable)


def _run_post_cleanup_gc(
    mngr_ctx: MngrContext,
    result: CleanupResult,
) -> None:
    """Run garbage collection after destroying agents."""
    try:
        with log_span("Running post-cleanup garbage collection"):
            providers = get_all_provider_instances(mngr_ctx)
            resource_types = GcResourceTypes(
                is_machines=True,
                is_work_dirs=True,
                is_snapshots=True,
                is_volumes=True,
                is_logs=False,
                is_build_cache=False,
            )
            gc_result = api_gc(
                mngr_ctx=mngr_ctx,
                providers=providers,
                resource_types=resource_types,
                dry_run=False,
                error_behavior=ErrorBehavior.CONTINUE,
            )
            if gc_result.errors:
                for error in gc_result.errors:
                    result.failures.append(
                        CleanupFailure(category=CleanupFailureCategory.OTHER, message=f"GC: {error}")
                    )
    except MngrError as e:
        error_msg = f"Post-cleanup garbage collection failed: {e}"
        logger.warning(error_msg)
        result.failures.append(CleanupFailure(category=CleanupFailureCategory.OTHER, message=error_msg))
