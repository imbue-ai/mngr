from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import wait
from datetime import datetime
from datetime import timezone
from typing import Final
from typing import TypeVar

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroupState
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.mngr.agents.compaction_transcript import COMPACTION_TRANSCRIPT_CACHE
from imbue.mngr.api.discover import DiscoveredAgent
from imbue.mngr.api.discover import DiscoveredHost
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNoLongerIdleError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.thread_cleanup import mngr_executor
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode

# How long a compaction waits for the agent's message lock: long enough to outlast a brief hold (a
# key chord, even under gVisor), short enough that a pass never stalls on a busy agent. Someone
# holding it longer is talking to the agent, so it is not idle and is skipped until a later pass.
COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS: Final[float] = 5.0

# How long a pass waits for its per-host and per-agent workers before warning about the ones still
# unfinished. Every command a worker runs is bounded, so this is reached only when something
# unforeseen hangs; the pass keeps waiting, since a worker thread cannot be stopped.
SLOW_WORKER_WARNING_SECONDS: Final[float] = 300.0

_ItemT = TypeVar("_ItemT")
_ResultT = TypeVar("_ResultT")


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
    """Check whether an agent is idle long enough to warrant context compaction before cache expiry."""
    return get_stale_idle_since(agent, config, expected_mode=expected_mode, now=now) is not None


def get_stale_idle_since(
    agent: AgentInterface,
    config: AutoCompactPluginConfig,
    expected_mode: ContextCompactionMode,
    now: datetime | None = None,
) -> datetime | None:
    """Return when the agent became idle if it is idle long enough to warrant compaction, else None.

    Returns the idle start only if:
    1. The agent implements HasCompactionMixin.
    2. The agent's compaction mode matches expected_mode.
    3. The agent has an active idle epoch (agent.get_idle_since() is not None).
    4. A cache TTL is known (either overridden in config or reported by the agent).
    5. (now - idle_since) >= trigger_delay_seconds.
    6. The agent's context size meets min_context_tokens (or min_context_tokens is 0).
       If get_context_tokens() is None and min_context_tokens > 0, the agent is not stale.
    7. The agent is running.

    The checks run cheapest first. The running check is last because its lifecycle probe
    spawns processes on the host, and almost every agent is rejected by an earlier check.
    """
    if not isinstance(agent, HasCompactionMixin):
        return None

    if config.mode != expected_mode:
        return None

    try:
        if not _is_concurrency_group_active(agent):
            return None

        idle_since = agent.get_idle_since()
        if idle_since is None:
            return None

        effective_ttl = config.cache_ttl_minutes or agent.get_cache_ttl_minutes()
        if effective_ttl is None:
            logger.debug(
                "Agent {} does not report cache TTL and no override configured; skipping compaction staleness check",
                agent.name,
            )
            return None

        current_time = now or datetime.now(timezone.utc)
        idle_duration_seconds = (current_time - idle_since).total_seconds()
        if idle_duration_seconds < config.get_trigger_delay_seconds(effective_ttl):
            return None

        if config.min_context_tokens > 0:
            context_tokens = agent.get_context_tokens()
            if context_tokens is None:
                logger.debug(
                    "Agent {} does not report context tokens; skipping compaction because min_context_tokens={}",
                    agent.name,
                    config.min_context_tokens,
                )
                return None
            if context_tokens < config.min_context_tokens:
                return None

        if not agent.is_running():
            logger.debug("Skipped compaction staleness check for agent {}: not running", agent.name)
            return None

        return idle_since
    except InvalidConcurrencyGroupStateError:
        return None
    except (MngrError, OSError) as e:
        logger.debug("Error during compaction staleness check for agent {}: {}", agent.name, e)
        return None


def trigger_compaction(
    agent: AgentInterface,
    instructions: str | None = None,
    # The idle start the decision to compact was based on; the agent is skipped if it has moved.
    expected_idle_since: datetime | None = None,
) -> bool:
    """Request context compaction on an agent if it implements HasCompactionMixin.

    Never waits long on a busy agent: if another send holds its message lock past a short
    timeout, or the agent is no longer idle once the lock is held, it is skipped until a later
    pass. Returns True if compaction was requested, False if aborted or skipped.
    """
    if not isinstance(agent, HasCompactionMixin):
        return False

    try:
        if not _is_concurrency_group_active(agent):
            return False
        logger.info("Triggering context compaction for agent {}", agent.name)
        agent.request_compaction(
            instructions=instructions,
            message_lock_timeout_seconds=COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS,
            expected_idle_since=expected_idle_since,
        )
        return True
    except InvalidConcurrencyGroupStateError:
        logger.debug(
            "Concurrency group is no longer active while triggering compaction for agent {}; aborting",
            agent.name,
        )
        return False
    except MessageLockTimeoutError as e:
        logger.debug("Skipped context compaction for agent {} until a later pass: {}", agent.name, e)
        return False
    except AgentNoLongerIdleError as e:
        logger.debug("Skipped context compaction for agent {}: {}", agent.name, e)
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
    stale_idle_since = get_stale_idle_since(agent, config, expected_mode=expected_mode, now=now)
    if stale_idle_since is None:
        return False

    return trigger_compaction(agent, instructions=instructions, expected_idle_since=stale_idle_since)


def _load_host_compaction_agents(
    host_ref: DiscoveredHost,
    agent_refs: list[DiscoveredAgent],
    mngr_ctx: MngrContext,
) -> list[AgentInterface]:
    """Load the compaction-supporting agents among the given refs on a single host, without loading its other agents."""
    try:
        provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
        host = provider.get_host(host_ref.host_id)
        if not isinstance(host, OnlineHostInterface):
            logger.debug("Skipped agents on host {} for compaction: host is offline", host_ref.host_name)
            return []

        live_agents = {a.id: a for a in host.load_agents_from_refs(agent_refs)}
        compaction_agents: list[AgentInterface] = []
        for agent_ref in agent_refs:
            live_agent = live_agents.get(agent_ref.agent_id)
            if live_agent is None:
                logger.debug(
                    "Skipped agent {} for compaction: not loaded on host {}", agent_ref.agent_name, host_ref.host_name
                )
            elif not isinstance(live_agent, HasCompactionMixin):
                logger.debug(
                    "Skipped agent {} for compaction: type {} does not support it",
                    agent_ref.agent_name,
                    live_agent.agent_type,
                )
            else:
                compaction_agents.append(live_agent)
        return compaction_agents
    except (MngrError, OSError) as e:
        logger.debug("Error checking agents on host {}: {}", host_ref.host_name, e)
        return []


def _run_workers(
    mngr_ctx: MngrContext,
    executor_name: str,
    items: Sequence[_ItemT],
    work: Callable[[_ItemT], _ResultT],
    describe_item: Callable[[_ItemT], str],
    slow_worker_warning_seconds: float = SLOW_WORKER_WARNING_SECONDS,
) -> list[_ResultT]:
    """Run ``work`` on every item in parallel and return every result, in item order.

    Waits for every worker. If any is unfinished after ``slow_worker_warning_seconds``, one warning
    names them all and the pass keeps waiting. A worker's exception propagates as-is (results are
    read after the executor exits, so it is not wrapped in a ConcurrencyExceptionGroup).
    """
    if not items:
        return []
    with mngr_executor(parent_cg=mngr_ctx.concurrency_group, name=executor_name, max_workers=32) as executor:
        futures = [executor.submit(work, item) for item in items]
        _done, unfinished = wait(futures, timeout=slow_worker_warning_seconds)
        if unfinished:
            logger.warning(
                "Autocompact workers are still unfinished after {:.0f}s for: {}. This pass keeps waiting for them",
                slow_worker_warning_seconds,
                ", ".join(
                    describe_item(item) for item, future in zip(items, futures, strict=True) if future in unfinished
                ),
            )
    return [future.result() for future in futures]


def _select_named_agent_refs(
    agents_by_host: Mapping[DiscoveredHost, Sequence[DiscoveredAgent]],
    names: Sequence[AgentName],
) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
    """Keep only the discovered agents with one of the given names, logging each name that matched none."""
    requested_names = set(names)
    selected_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
    for host_ref, agent_refs in agents_by_host.items():
        selected_refs = [agent_ref for agent_ref in agent_refs if agent_ref.agent_name in requested_names]
        if selected_refs:
            selected_by_host[host_ref] = selected_refs

    found_names = {agent_ref.agent_name for agent_refs in selected_by_host.values() for agent_ref in agent_refs}
    for name in sorted(requested_names - found_names):
        logger.debug("Skipped agent {} for compaction: no discovered agent has that name", name)
    return selected_by_host


def get_compaction_agents(
    mngr_ctx: MngrContext,
    names: Sequence[AgentName] | None = None,
) -> list[AgentInterface]:
    """Discover all agents supporting compaction on online hosts, whether or not they are running.

    When names is given, only agents with those names are loaded, and a name that matches no
    discovered agent is skipped rather than treated as an error.
    """
    outcome = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None if names is None else tuple(str(name) for name in names),
        include_destroyed=False,
        reset_caches=False,
    )
    agents_by_host = (
        outcome.agents_by_host if names is None else _select_named_agent_refs(outcome.agents_by_host, names)
    )

    agents_by_loaded_host = _run_workers(
        mngr_ctx=mngr_ctx,
        executor_name="autocompact_load_host_agents",
        items=list(agents_by_host.items()),
        work=lambda host_entry: _load_host_compaction_agents(host_entry[0], host_entry[1], mngr_ctx),
        describe_item=lambda host_entry: f"host {host_entry[0].host_name}",
    )
    return [agent for host_agents in agents_by_loaded_host for agent in host_agents]


def _evaluate_single_agent(
    agent: AgentInterface,
    action: Callable[[AgentInterface, AutoCompactPluginConfig], bool],
) -> AgentName | None:
    """Evaluate a single agent against an action, returning its name if matching."""
    config = agent.mngr_ctx.get_plugin_config("autocompact", AutoCompactPluginConfig)
    if action(agent, config):
        return agent.name
    return None


def _evaluate_stale_agents(
    mngr_ctx: MngrContext,
    action: Callable[[AgentInterface, AutoCompactPluginConfig], bool],
    agents: Sequence[AgentInterface] | None = None,
) -> list[AgentName]:
    """Evaluate candidate agents against an action, returning matching agent names.

    Cached transcript scans of agents outside this evaluation are evicted, so the cache holds only
    agents that are still being checked.
    """
    candidate_agents = get_compaction_agents(mngr_ctx) if agents is None else agents
    COMPACTION_TRANSCRIPT_CACHE.evict_agents_other_than(frozenset(agent.id for agent in candidate_agents))

    evaluated_names = _run_workers(
        mngr_ctx=mngr_ctx,
        executor_name="autocompact_evaluate_agents",
        items=candidate_agents,
        work=lambda agent: _evaluate_single_agent(agent, action),
        describe_item=lambda agent: f"agent {agent.name}",
    )
    return [name for name in evaluated_names if name is not None]


def get_stale_agents(
    mngr_ctx: MngrContext,
    agents: Sequence[AgentInterface] | None = None,
    now: datetime | None = None,
) -> list[AgentName]:
    """Discover running agents across online hosts (or evaluate provided agents) and return names of stale agents."""
    return _evaluate_stale_agents(
        mngr_ctx,
        lambda agent, config: is_agent_stale_for_compaction(
            agent,
            config,
            expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
            now=now,
        ),
        agents=agents,
    )


def compact_stale_agents(
    mngr_ctx: MngrContext,
    agents: Sequence[AgentInterface] | None = None,
    now: datetime | None = None,
    instructions: str | None = None,
) -> list[AgentName]:
    """Discover and compact stale agents across online hosts (or evaluate and compact provided agents)."""
    return _evaluate_stale_agents(
        mngr_ctx,
        lambda agent, config: compact_agent_if_stale(
            agent,
            config,
            expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
            now=now,
            instructions=instructions,
        ),
        agents=agents,
    )


def compact_stale_agents_by_name(
    mngr_ctx: MngrContext,
    names: Sequence[AgentName],
    now: datetime | None = None,
) -> list[AgentName]:
    """Compact whichever of the named agents are stale, returning the names that were compacted.

    This is the entry point for code that embeds mngr. Such a caller lists its agents a moment
    before calling, so a named agent may have stopped or been destroyed in between. Unlike
    `mngr autocompact run <targets>`, this does not raise for such a name: a name matching no discovered
    agent, an agent on an offline host or a host whose load fails with MngrError or OSError, an agent
    without compaction support, and an agent that is not running are each skipped with a debug log.
    Any other exception while loading a host or evaluating an agent (for example a corrupt agent
    record) propagates and discards the result of the whole call, so an embedder should catch
    Exception at its own boundary.

    Every agent with one of the names is a candidate, on whichever host or provider it lives. A name
    that matches nothing costs a full discovery scan, so callers should pass names they have just
    listed. Provider instances are cached per MngrContext: a caller that builds a context per call
    must release them with `close_provider_instances_for_context(mngr_ctx)` afterwards.
    """
    if not names:
        return []
    # Resolved through discovery rather than find_all_agents, which fails the whole batch when any one
    # name is unmatched.
    agents = get_compaction_agents(mngr_ctx, names=names)
    return compact_stale_agents(mngr_ctx, agents=agents, now=now)
