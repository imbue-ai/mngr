from datetime import datetime
from datetime import timezone

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroupState
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode


def _is_concurrency_group_active(agent: AgentInterface) -> bool:
    """Return False if agent's concurrency group is exited or shutting down."""
    if hasattr(agent, "mngr_ctx") and agent.mngr_ctx is not None:
        if hasattr(agent.mngr_ctx, "concurrency_group"):
            cg = agent.mngr_ctx.concurrency_group
            if cg is not None:
                if cg.state not in (ConcurrencyGroupState.ACTIVE, ConcurrencyGroupState.INSTANTIATED):
                    return False
                if cg.is_shutting_down():
                    return False
    return True


def is_agent_stale_for_compaction(
    agent: AgentInterface,
    config: AutoCompactPluginConfig,
    expected_mode: ContextCompactionMode,
    now: datetime | None = None,
) -> bool:
    """Check whether an agent is idle long enough to warrant context compaction before cache expiry.

    Returns True only if:
    1. The agent implements HasCompactionMixin.
    2. The agent's compaction mode matches expected_mode.
    3. The agent is running and has an active idle epoch (agent.get_idle_since() is not None).
    4. The agent's context size meets min_context_tokens (or min_context_tokens is 0).
       If get_context_tokens() is None and min_context_tokens > 0, returns False and logs a warning.
    5. A cache TTL is known (either overridden in config or reported by the agent).
    6. (now - idle_since) >= trigger_delay_seconds.
    """
    if not isinstance(agent, HasCompactionMixin):
        return False

    if config.mode != expected_mode:
        return False

    try:
        if not _is_concurrency_group_active(agent):
            return False

        if not agent.is_running():
            return False

        idle_since = agent.get_idle_since()
        if idle_since is None:
            return False

        effective_ttl = config.cache_ttl_minutes or agent.get_cache_ttl_minutes()
        if effective_ttl is None:
            logger.debug(
                "Agent {} does not report cache TTL and no override configured; skipping compaction staleness check",
                agent.name,
            )
            return False

        if config.min_context_tokens > 0:
            context_tokens = agent.get_context_tokens()
            if context_tokens is None:
                logger.warning(
                    "Agent {} does not report context tokens; skipping compaction because min_context_tokens={}",
                    agent.name,
                    config.min_context_tokens,
                )
                return False
            if context_tokens < config.min_context_tokens:
                return False

        now = now or datetime.now(timezone.utc)
        trigger_delay_seconds = config.get_trigger_delay_seconds(effective_ttl)
        idle_duration_seconds = (now - idle_since).total_seconds()
        return idle_duration_seconds >= trigger_delay_seconds
    except InvalidConcurrencyGroupStateError:
        return False
    except (MngrError, OSError) as e:
        logger.debug("Error during compaction staleness check for agent {}: {}", agent.name, e)
        return False


def trigger_compaction(agent: AgentInterface, instructions: str | None = None) -> bool:
    """Request context compaction on an agent if it implements HasCompactionMixin.

    Returns True if compaction was requested, False if aborted or skipped.
    """
    if not isinstance(agent, HasCompactionMixin):
        return False

    try:
        if not _is_concurrency_group_active(agent):
            return False
        logger.info("Triggering context compaction for agent {}", agent.name)
        agent.request_compaction(instructions=instructions)
        return True
    except InvalidConcurrencyGroupStateError:
        logger.debug(
            "Concurrency group is no longer active while triggering compaction for agent {}; aborting",
            agent.name,
        )
        return False
    except (MngrError, OSError) as e:
        logger.warning("Error triggering context compaction for agent {}: {}", agent.name, e)
        return False


def compact_agent_if_stale(
    agent: AgentInterface,
    config: AutoCompactPluginConfig,
    expected_mode: ContextCompactionMode,
    now: datetime | None = None,
    instructions: str | None = None,
) -> bool:
    """Check if an agent is stale, and if so trigger context compaction.

    Returns True if compaction was triggered, False otherwise.
    """
    if not is_agent_stale_for_compaction(agent, config, expected_mode=expected_mode, now=now):
        return False

    return trigger_compaction(agent, instructions=instructions)


def get_stale_agents(
    mngr_ctx: MngrContext,
    now: datetime | None = None,
) -> list[AgentName]:
    """Discover running agents across all online hosts and return names of stale agents."""
    outcome = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    stale: list[AgentName] = []

    for host_ref, agent_refs in outcome.agents_by_host.items():
        try:
            provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
            host = provider.get_host(host_ref.host_id)
            if not isinstance(host, OnlineHostInterface):
                continue

            live_agents = {a.id: a for a in host.get_agents()}
            for agent_ref in agent_refs:
                live_agent = live_agents.get(agent_ref.agent_id)
                if live_agent is None or not isinstance(live_agent, HasCompactionMixin):
                    continue

                config = live_agent.mngr_ctx.get_plugin_config("autocompact", AutoCompactPluginConfig)
                if is_agent_stale_for_compaction(
                    live_agent,
                    config,
                    expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
                    now=now,
                ):
                    stale.append(live_agent.name)
        except (MngrError, OSError) as e:
            logger.debug("Error checking agents on host {}: {}", host_ref.host_name, e)

    return stale


def compact_all_agents(
    mngr_ctx: MngrContext,
    now: datetime | None = None,
    instructions: str | None = None,
) -> list[AgentName]:
    """Discover and compact all stale agents across all online hosts."""
    outcome = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    compacted: list[AgentName] = []

    for host_ref, agent_refs in outcome.agents_by_host.items():
        try:
            provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
            host = provider.get_host(host_ref.host_id)
            if not isinstance(host, OnlineHostInterface):
                continue

            live_agents = {a.id: a for a in host.get_agents()}
            for agent_ref in agent_refs:
                live_agent = live_agents.get(agent_ref.agent_id)
                if live_agent is None or not isinstance(live_agent, HasCompactionMixin):
                    continue

                config = live_agent.mngr_ctx.get_plugin_config("autocompact", AutoCompactPluginConfig)
                if compact_agent_if_stale(
                    live_agent,
                    config,
                    expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
                    now=now,
                    instructions=instructions,
                ):
                    compacted.append(live_agent.name)
        except (MngrError, OSError) as e:
            logger.debug("Error checking agents on host {}: {}", host_ref.host_name, e)

    return compacted
