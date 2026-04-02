"""Agent and host launching for the test-mapreduce plugin."""

import math
import time

from loguru import logger

from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.create import resolve_target_host
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import HostLocation
from imbue.mngr.interfaces.host import AgentDataOptions
from imbue.mngr.interfaces.host import AgentGitOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import NewHostBuildOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import SnapshotName
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.prompts import build_integrator_prompt
from imbue.mngr_tmr.prompts import build_test_agent_prompt
from imbue.mngr_tmr.utils import resolve_templates
from imbue.mngr_tmr.utils import sanitize_test_name_for_agent
from imbue.mngr_tmr.utils import short_random_id
from imbue.mngr_tmr.utils import transfer_mode_for_provider

_AGENT_CREATION_TIMEOUT_SECONDS = 600.0


def build_agent_options(
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    initial_message: str | None = None,
) -> CreateAgentOptions:
    """Build CreateAgentOptions for a tmr agent."""
    transfer_mode = transfer_mode_for_provider(config.provider_name)
    is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
    return CreateAgentOptions(
        agent_type=config.agent_type,
        name=agent_name,
        initial_message=initial_message,
        transfer_mode=transfer_mode,
        git=AgentGitOptions(
            new_branch_name=branch_name,
        ),
        data_options=AgentDataOptions(is_rsync_enabled=False),
        environment=config.env_options,
        label_options=config.label_options,
        ready_timeout_seconds=60.0 if is_remote else 10.0,
    )


def _create_tmr_agent(
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    initial_message: str | None = None,
    existing_host: OnlineHostInterface | None = None,
    host_name: HostName | None = None,
) -> CreateAgentResult:
    """Create an agent on the configured provider with an optional initial message.

    If existing_host is provided, the agent is placed on that host instead of
    creating a new one (used for host sharing in remote providers).
    """
    agent_options = build_agent_options(agent_name, branch_name, config, initial_message=initial_message)
    source_location = HostLocation(host=config.source_host, path=config.source_dir)

    if existing_host is not None:
        target_host: OnlineHostInterface | NewHostOptions = existing_host
    else:
        tmpl = resolve_templates(config.templates, mngr_ctx.config) if config.templates else {}
        snapshot = config.snapshot
        raw_build_args = tmpl.get("build_args", ())
        raw_start_args = tmpl.get("start_args", ())
        build_args = tuple(str(a) for a in raw_build_args) if isinstance(raw_build_args, (list, tuple)) else ()
        start_args = tuple(str(a) for a in raw_start_args) if isinstance(raw_start_args, (list, tuple)) else ()
        build = NewHostBuildOptions(snapshot=snapshot, build_args=build_args, start_args=start_args)
        is_local = config.provider_name.lower() == LOCAL_PROVIDER_NAME
        resolved_host_name = None if is_local else host_name
        target_host = NewHostOptions(provider=config.provider_name, name=resolved_host_name, build=build)

    return api_create(
        source_location=source_location,
        target_host=target_host,
        agent_options=agent_options,
        mngr_ctx=mngr_ctx,
    )


def launch_test_agent(
    test_node_id: str,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str = "",
    existing_host: OnlineHostInterface | None = None,
    host_name: HostName | None = None,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch a single agent to run and optionally fix one test."""
    agent_name_suffix = sanitize_test_name_for_agent(test_node_id)
    short_id = short_random_id()
    agent_name = AgentName(f"tmr-{agent_name_suffix}-{short_id}")

    logger.info("Launching agent '{}' for test: {}", agent_name, test_node_id)
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mngr-tmr/{agent_name_suffix}-{short_id}",
        config=config,
        mngr_ctx=mngr_ctx,
        initial_message=build_test_agent_prompt(test_node_id, pytest_flags, prompt_suffix),
        existing_host=existing_host,
        host_name=host_name,
    )

    branch = f"mngr-tmr/{agent_name_suffix}-{short_id}"
    return (
        TestAgentInfo(
            test_node_id=test_node_id,
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            work_dir=create_result.agent.work_dir,
            branch_name=branch,
            created_at=time.monotonic(),
        ),
        create_result.host,
    )


def _create_snapshot_host(
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
) -> SnapshotName:
    """Launch a dedicated snapshotter agent, snapshot its host, then stop it."""
    short_id = short_random_id()
    agent_name = AgentName(f"tmr-snapshotter-{short_id}")

    logger.info("Launching snapshotter agent '{}' for provisioning...", agent_name)
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mngr-tmr/snapshotter-{short_id}",
        config=config,
        mngr_ctx=mngr_ctx,
    )

    snapshotter_host = create_result.host
    snapshotter_agent_id = create_result.agent.id

    try:
        provider = get_provider_instance(config.provider_name, mngr_ctx)
        snapshot_id = provider.create_snapshot(snapshotter_host)
        snapshot_name = SnapshotName(str(snapshot_id))
        logger.info("Created snapshot '{}' from snapshotter host", snapshot_name)
        return snapshot_name
    finally:
        stop_agent_on_host(snapshotter_host, snapshotter_agent_id, agent_name)


def stop_agent_on_host(host: OnlineHostInterface, agent_id: AgentId, agent_name: AgentName) -> None:
    """Stop a single agent on the host."""
    try:
        host.stop_agents([agent_id])
        logger.info("Stopped agent '{}'", agent_name)
    except (MngrError, HostError) as exc:
        logger.warning("Failed to stop agent '{}': {}", agent_name, exc)


def _create_host_pool(
    host_count: int,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    run_name: str,
    max_parallel: int,
) -> list[OnlineHostInterface]:
    """Pre-create a pool of hosts for remote agent placement."""
    hosts: list[OnlineHostInterface] = []
    tmpl = resolve_templates(config.templates, mngr_ctx.config) if config.templates else {}
    snapshot = config.snapshot
    raw_build_args = tmpl.get("build_args", ())
    raw_start_args = tmpl.get("start_args", ())
    build_args = tuple(str(a) for a in raw_build_args) if isinstance(raw_build_args, (list, tuple)) else ()
    start_args = tuple(str(a) for a in raw_start_args) if isinstance(raw_start_args, (list, tuple)) else ()
    build = NewHostBuildOptions(snapshot=snapshot, build_args=build_args, start_args=start_args)

    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group,
        name="tmr_create_hosts",
        max_workers=max_parallel,
    ) as executor:
        futures = []
        for i in range(host_count):
            h_name = HostName(f"{run_name}-host-{i}")
            new_host_opts = NewHostOptions(provider=config.provider_name, name=h_name, build=build)
            futures.append(executor.submit(resolve_target_host, new_host_opts, mngr_ctx))
        for future in futures:
            try:
                hosts.append(future.result())
            except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to create host: {}", exc)

    logger.info("Created {} host(s) for agent placement", len(hosts))
    return hosts


def launch_all_test_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str = "",
    use_snapshot: bool = False,
    max_parallel: int = 4,
    launch_delay_seconds: float = 2.0,
    agents_per_host: int = 4,
    run_name: str = "tmr",
) -> tuple[list[TestAgentInfo], dict[str, OnlineHostInterface], SnapshotName | None]:
    """Launch agents for all collected tests.

    For remote providers, agents_per_host controls how many agents share a single
    host. Hosts are pre-created in a pool and agents are assigned round-robin.
    For local providers, this setting is ignored (all agents share localhost).
    """
    agents: list[TestAgentInfo] = []
    agent_hosts: dict[str, OnlineHostInterface] = {}

    launch_config = config
    if use_snapshot:
        provider = get_provider_instance(config.provider_name, mngr_ctx)
        if provider.supports_snapshots:
            try:
                snapshot_name = _create_snapshot_host(config, mngr_ctx)
                launch_config = config.model_copy_update(to_update(config.field_ref().snapshot, snapshot_name))
            except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to create snapshot, launching agents without snapshot: {}", exc)
        else:
            logger.warning(
                "Provider '{}' does not support snapshots, launching all agents without snapshot",
                config.provider_name,
            )

    is_local = launch_config.provider_name.lower() == LOCAL_PROVIDER_NAME
    host_pool: list[OnlineHostInterface] = []
    if not is_local and agents_per_host > 0:
        host_count = math.ceil(len(test_node_ids) / agents_per_host)
        if host_count > 0:
            host_pool = _create_host_pool(host_count, launch_config, mngr_ctx, run_name, max_parallel)

    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group,
        name="tmr_launch",
        max_workers=max_parallel,
    ) as executor:
        futures = []
        for i, test_node_id in enumerate(test_node_ids):
            if i > 0 and launch_delay_seconds > 0:
                time.sleep(launch_delay_seconds)
            existing_host = host_pool[i % len(host_pool)] if host_pool else None
            h_name = HostName(f"{run_name}-host-{i}") if not is_local and not host_pool else None
            futures.append(
                executor.submit(
                    launch_test_agent,
                    test_node_id,
                    launch_config,
                    mngr_ctx,
                    pytest_flags,
                    prompt_suffix,
                    existing_host,
                    h_name,
                )
            )
        for future in futures:
            try:
                info, host = future.result()
                agents.append(info)
                agent_hosts[str(info.agent_id)] = host
            except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to launch agent: {}", exc)

    logger.info("Launched {} agent(s)", len(agents))
    return agents, agent_hosts, launch_config.snapshot


def launch_with_timeout(
    test_node_id: str,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch a test agent with a timeout. Raises TimeoutError if creation takes too long."""
    with ConcurrencyGroupExecutor(mngr_ctx.concurrency_group, name="launch-agent", max_workers=1) as executor:
        future = executor.submit(launch_test_agent, test_node_id, config, mngr_ctx, pytest_flags, prompt_suffix)
        return future.result(timeout=_AGENT_CREATION_TIMEOUT_SECONDS)


def launch_agents_up_to_limit(
    remaining_tests: list[str],
    pending_ids: set[str],
    max_agents: int,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
    agent_id_to_info: dict[str, TestAgentInfo],
) -> None:
    """Launch agents from remaining_tests until we hit max_agents running.

    Mutates remaining_tests (pops from front), pending_ids, all_agents,
    all_hosts, and agent_id_to_info in place.
    """
    while remaining_tests and (max_agents <= 0 or len(pending_ids) < max_agents):
        test_node_id = remaining_tests.pop(0)
        try:
            info, host = launch_with_timeout(test_node_id, config, mngr_ctx, pytest_flags, prompt_suffix)
        except TimeoutError:
            logger.warning("Agent creation timed out after {}s for {}", _AGENT_CREATION_TIMEOUT_SECONDS, test_node_id)
            continue
        except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
            logger.warning("Failed to launch agent for {}: {}", test_node_id, exc)
            continue
        all_agents.append(info)
        all_hosts[str(info.agent_id)] = host
        agent_id_to_info[str(info.agent_id)] = info
        pending_ids.add(str(info.agent_id))


def launch_integrator_agent(
    fix_branches: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch an integrator agent that cherry-picks fix branches into a linear stack."""
    short_id = short_random_id()
    agent_name = AgentName(f"tmr-integrator-{short_id}")
    prompt = build_integrator_prompt(fix_branches)

    logger.info("Launching integrator agent '{}' to integrate {} branches", agent_name, len(fix_branches))
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mngr-tmr/integrated-{short_id}",
        config=config,
        mngr_ctx=mngr_ctx,
        initial_message=prompt,
    )

    return (
        TestAgentInfo(
            test_node_id="integrator",
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            work_dir=create_result.agent.work_dir,
            branch_name=f"mngr-tmr/integrated-{short_id}",
            created_at=time.monotonic(),
        ),
        create_result.host,
    )
