from typing import assert_never

from loguru import logger

from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.mng.api.data_types import CleanupResult
from imbue.mng.api.data_types import GcResourceTypes
from imbue.mng.api.discovery_events import emit_agent_destroyed
from imbue.mng.api.discovery_events import emit_discovery_events_for_host
from imbue.mng.api.discovery_events import emit_host_destroyed
from imbue.mng.api.gc import gc as api_gc
from imbue.mng.api.list import list_agents
from imbue.mng.api.providers import get_all_provider_instances
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CleanupAction
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId


@log_call
def find_agents_for_cleanup(
    mng_ctx: MngContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    error_behavior: ErrorBehavior,
) -> list[AgentDetails]:
    """Find agents matching the given filters for cleanup."""
    result = list_agents(
        mng_ctx=mng_ctx,
        is_streaming=False,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        error_behavior=error_behavior,
    )
    return result.agents


@log_call
def execute_cleanup(
    mng_ctx: MngContext,
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
            _execute_destroy(mng_ctx, agents_by_host, result, error_behavior)
        case CleanupAction.STOP:
            _execute_stop(mng_ctx, agents_by_host, result, error_behavior)
        case _ as unreachable:
            assert_never(unreachable)

    # Run post-destroy GC when destroying
    if action == CleanupAction.DESTROY and result.destroyed_agents:
        _run_post_cleanup_gc(mng_ctx, result)

    return result


def _execute_destroy(
    mng_ctx: MngContext,
    agents_by_host: dict[HostId, list[AgentDetails]],
    result: CleanupResult,
    error_behavior: ErrorBehavior,
) -> None:
    """Destroy agents, grouped by host."""
    for host_id, host_agents in agents_by_host.items():
        provider_name = host_agents[0].host.provider_name
        try:
            provider = get_provider_instance(provider_name, mng_ctx)
            host = provider.get_host(host_id)

            match host:
                case OnlineHostInterface() as online_host:
                    with log_span("Destroying agents on online host {}", host_id):
                        for agent_details in host_agents:
                            try:
                                # Find the agent interface on the host
                                for agent in online_host.get_agents():
                                    if agent.id == agent_details.id:
                                        mng_ctx.pm.hook.on_before_agent_destroy(agent=agent, host=online_host)
                                        online_host.destroy_agent(agent)
                                        mng_ctx.pm.hook.on_agent_destroyed(agent=agent, host=online_host)
                                        result.destroyed_agents.append(agent_details.name)
                                        logger.debug("Destroyed agent: {}", agent_details.name)
                                        emit_agent_destroyed(mng_ctx.config, agent_details.id, host_id)
                                        emit_discovery_events_for_host(mng_ctx.config, online_host)
                                        break
                                else:
                                    # Agent not found on host (likely already cleaned up)
                                    logger.debug(
                                        "Agent {} not found on host, treating as already destroyed",
                                        agent_details.name,
                                    )
                                    result.destroyed_agents.append(agent_details.name)
                            except MngError as e:
                                error_msg = f"Error destroying agent {agent_details.name}: {e}"
                                logger.warning(error_msg)
                                result.errors.append(error_msg)
                                if error_behavior == ErrorBehavior.ABORT:
                                    return
                case HostInterface() as offline_host:
                    with log_span("Destroying offline host {} with {} agent(s)", host_id, len(host_agents)):
                        try:
                            mng_ctx.pm.hook.on_before_host_destroy(host=offline_host)
                            provider.destroy_host(offline_host)
                            mng_ctx.pm.hook.on_host_destroyed(host=offline_host)
                            for agent_details in host_agents:
                                result.destroyed_agents.append(agent_details.name)
                                logger.debug("Destroyed agent: {} (via host destruction)", agent_details.name)
                            emit_host_destroyed(mng_ctx.config, host_id, [ad.id for ad in host_agents])
                        except MngError as e:
                            error_msg = f"Error destroying offline host {host_id}: {e}"
                            logger.warning(error_msg)
                            result.errors.append(error_msg)
                            if error_behavior == ErrorBehavior.ABORT:
                                return
                case _ as unreachable:
                    assert_never(unreachable)
        except MngError as e:
            error_msg = f"Error accessing host {host_id}: {e}"
            logger.warning(error_msg)
            result.errors.append(error_msg)
            if error_behavior == ErrorBehavior.ABORT:
                return


def _execute_stop(
    mng_ctx: MngContext,
    agents_by_host: dict[HostId, list[AgentDetails]],
    result: CleanupResult,
    error_behavior: ErrorBehavior,
) -> None:
    """Stop agents, grouped by host."""
    for host_id, host_agents in agents_by_host.items():
        provider_name = host_agents[0].host.provider_name
        try:
            provider = get_provider_instance(provider_name, mng_ctx)
            host = provider.get_host(host_id)

            match host:
                case OnlineHostInterface() as online_host:
                    with log_span("Stopping agents on host {}", host_id):
                        agent_ids_to_stop = [agent_details.id for agent_details in host_agents]
                        try:
                            online_host.stop_agents(agent_ids_to_stop)
                            for agent_details in host_agents:
                                result.stopped_agents.append(agent_details.name)
                                logger.debug("Stopped agent: {}", agent_details.name)
                        except MngError as e:
                            error_msg = f"Error stopping agents on host {host_id}: {e}"
                            logger.warning(error_msg)
                            result.errors.append(error_msg)
                            if error_behavior == ErrorBehavior.ABORT:
                                return
                case HostInterface():
                    warning_msg = (
                        f"Skipping {len(host_agents)} agent(s) on offline host {host_id} "
                        "(cannot stop agents on offline hosts)"
                    )
                    logger.warning(warning_msg)
                    result.errors.append(warning_msg)
                case _ as unreachable:
                    assert_never(unreachable)
        except MngError as e:
            error_msg = f"Error accessing host {host_id}: {e}"
            logger.warning(error_msg)
            result.errors.append(error_msg)
            if error_behavior == ErrorBehavior.ABORT:
                return


def _run_post_cleanup_gc(
    mng_ctx: MngContext,
    result: CleanupResult,
) -> None:
    """Run garbage collection after destroying agents."""
    try:
        with log_span("Running post-cleanup garbage collection"):
            providers = get_all_provider_instances(mng_ctx)
            resource_types = GcResourceTypes(
                is_machines=True,
                is_work_dirs=True,
                is_snapshots=True,
                is_volumes=True,
                is_logs=False,
                is_build_cache=False,
            )
            gc_result = api_gc(
                mng_ctx=mng_ctx,
                providers=providers,
                resource_types=resource_types,
                dry_run=False,
                error_behavior=ErrorBehavior.CONTINUE,
            )
            if gc_result.errors:
                for error in gc_result.errors:
                    result.errors.append(f"GC: {error}")
    except MngError as e:
        error_msg = f"Post-cleanup garbage collection failed: {e}"
        logger.warning(error_msg)
        result.errors.append(error_msg)
