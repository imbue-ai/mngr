"""Agent and host launching for the mngr-mapreduce framework."""

import math
import time
from concurrent.futures import Future
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.create import bootstrap_backend_for_host_creation
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.create import resolve_target_host
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.rsync import rsync_to_remote
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import require_interactive_agent
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.host import AgentDataOptions
from imbue.mngr.interfaces.host import AgentGitOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostEnvironmentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostBuildOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import LaunchConfig
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceRecipe
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.data_types import MapperInfo
from imbue.mngr_mapreduce.data_types import ReducerInfo
from imbue.mngr_mapreduce.utils import dedup_name
from imbue.mngr_mapreduce.utils import resolve_templates
from imbue.mngr_mapreduce.utils import sanitize_for_agent_name

_AGENT_CREATION_TIMEOUT_SECONDS = 600.0

# Hardcoded location of the code repo on every snapshotter-built host. The
# snapshotter agent's work_dir is placed here so that subsequent agents created
# from the snapshot can git-worktree off this same-host checkout instead of
# re-uploading the source from the laptop.
_HOST_CODE_DIR = Path("/code")

# Subdirectory of the reducer's work_dir into which the orchestrator rsyncs
# the local output directory before kicking the reducer off. Recipes that need
# to reference this from their reducer prompt can import this constant.
REDUCER_INPUTS_DIRNAME = ".mapreduce_inputs"

# Label key the framework stamps on every launched agent to classify it
# within the run. Value is an ``AgentKind`` string.
ROLE_LABEL_KEY = "mapreduce_role"


def _make_mapper_identity(
    recipe_name: str, run_name: str, task: MapReduceTask, used_suffixes: set[str]
) -> tuple[AgentName, str]:
    """Generate (agent_name, branch_name) for a mapper agent.

    The names are derived deterministically from ``(recipe_name, run_name,
    task)`` so the launch attempt and any failure-report entry share the
    same name. ``used_suffixes`` is mutated with the suffix chosen for this
    task; if sanitization-truncation would have collapsed two distinct
    task ids onto the same suffix, ``-2`` / ``-3`` / ... is appended to
    keep names unique within the run.
    """
    sanitized = sanitize_for_agent_name(task.slug_source())
    suffix = dedup_name(sanitized, used_suffixes)
    return AgentName(f"{recipe_name}-{run_name}-{suffix}"), f"{recipe_name}/{run_name}/{suffix}"


def _make_launch_failure_metadata(
    task_id: str, agent_name: AgentName, branch_name: str, error: object
) -> AgentMetadata:
    """Build an AgentMetadata marking that an agent failed to launch.

    Used so launch failures still appear in the report instead of silently
    disappearing.
    """
    return AgentMetadata(
        kind=AgentKind.MAPPER,
        agent_name=agent_name,
        task_id=task_id,
        branch_name=branch_name,
        error_summary=f"Failed to launch agent: {error}",
    )


def _resolve_build_options(config: LaunchConfig, mngr_ctx: MngrContext) -> NewHostBuildOptions:
    """Resolve templates and build NewHostBuildOptions for an agent or host pool entry."""
    tmpl = resolve_templates(config.templates, mngr_ctx.config) if config.templates else {}
    raw_build_args = tmpl.get("build_args", ())
    raw_start_args = tmpl.get("start_args", ())
    build_args = tuple(str(a) for a in raw_build_args) if isinstance(raw_build_args, (list, tuple)) else ()
    start_args = tuple(str(a) for a in raw_start_args) if isinstance(raw_start_args, (list, tuple)) else ()
    return NewHostBuildOptions(snapshot=config.snapshot, build_args=build_args, start_args=start_args)


def _build_host_environment(config: LaunchConfig) -> HostEnvironmentOptions:
    """Build HostEnvironmentOptions for hosts created by the framework."""
    return HostEnvironmentOptions(authorized_keys=config.additional_authorized_keys)


def _build_agent_options(
    agent_name: AgentName,
    branch_name: str,
    config: LaunchConfig,
    kind: AgentKind,
    initial_message: str | None = None,
    target_path: Path | None = None,
    transfer_mode: TransferMode = TransferMode.GIT_MIRROR,
) -> CreateAgentOptions:
    """Build CreateAgentOptions for a map-reduce agent.

    ``kind`` is stamped onto ``label_options`` as the ``mapreduce_role`` label,
    overriding any prior value carried on ``config``.

    ``target_path`` overrides where the agent's work_dir is placed on the
    host (used to pin the snapshotter to ``/code``). ``transfer_mode``
    defaults to ``GIT_MIRROR`` for every provider (including local) so that
    agent branches are kept in the agent's own clone and surface in the
    source repo only via the published bundle -- the orchestrator code path
    is then identical across providers. Callers override to ``GIT_WORKTREE``
    when source and target live on the same host (e.g. when an agent built
    from a snapshot sources from the host's own ``/code``).
    """
    is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
    label_options = AgentLabelOptions(labels={**config.label_options.labels, ROLE_LABEL_KEY: kind.value})
    return CreateAgentOptions(
        agent_type=config.agent_type,
        name=agent_name,
        initial_message=initial_message,
        target_path=target_path,
        transfer_mode=transfer_mode,
        git=AgentGitOptions(
            base_branch=config.base_commit,
            new_branch_name=branch_name,
        ),
        data_options=AgentDataOptions(is_rsync_enabled=False),
        environment=config.env_options,
        label_options=label_options,
        ready_timeout_seconds=60.0 if is_remote else 10.0,
    )


def _create_agent(
    agent_name: AgentName,
    branch_name: str,
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    kind: AgentKind,
    initial_message: str | None = None,
    existing_host: OnlineHostInterface | None = None,
    host_name: HostName | None = None,
) -> CreateAgentResult:
    """Create an agent on the configured provider with an optional initial message.

    ``kind`` classifies the agent -- it controls the ``mapreduce_role`` label
    (set by ``_build_agent_options``) and triggers the snapshotter-specific
    work_dir pinning when ``kind is AgentKind.SNAPSHOTTER``.

    If existing_host is provided, the agent is placed on that host instead of
    creating a new one (used for host sharing in remote providers).

    Snapshotter agents have their work_dir pinned to ``/code`` so the git
    repo on the snapshotter's host lives at a known path; subsequent agents
    created from the snapshot then git-worktree off that path instead of
    re-uploading the source.

    If the agent is being placed on a host built from a snapshot, source from
    that host's ``/code`` via git-worktree -- avoiding network roundtrips
    to upload code that's already there. When no ``existing_host`` is passed
    but ``config.snapshot`` is set, the host is pre-created here so the same
    optimization applies (this is how the reducer benefits, since it doesn't
    share a host pool with the mappers).
    """
    if existing_host is not None:
        target_host: OnlineHostInterface | NewHostOptions = existing_host
    elif config.provider_name.lower() == LOCAL_PROVIDER_NAME:
        # The local provider has a single fixed host ("localhost"); reuse the
        # source_host (already the local host) instead of the new-host path.
        # That path would call _generate_unique_host_name, which never finds
        # a free name because the local provider's get_host_name always
        # returns "localhost" and discover_hosts always reports it as taken.
        target_host = config.source_host
    else:
        build = _resolve_build_options(config, mngr_ctx)
        new_host_options = NewHostOptions(
            provider=config.provider_name,
            name=host_name,
            build=build,
            environment=_build_host_environment(config),
        )
        # When the host will be built from a snapshot, its /code already
        # contains the source. Pre-create the host so the GIT_WORKTREE branch
        # below sources from it instead of re-uploading from the laptop. The
        # snapshotter is excluded since it's the agent that populates /code.
        if config.snapshot is not None and kind is not AgentKind.SNAPSHOTTER:
            existing_host = resolve_target_host(new_host_options, mngr_ctx)
            target_host = existing_host
        else:
            target_host = new_host_options

    if kind is AgentKind.SNAPSHOTTER:
        source_location = HostLocation(host=config.source_host, path=config.source_dir)
        agent_options = _build_agent_options(
            agent_name,
            branch_name,
            config,
            kind,
            initial_message=initial_message,
            target_path=_HOST_CODE_DIR,
        )
    elif existing_host is not None and config.snapshot is not None:
        source_location = HostLocation(host=existing_host, path=_HOST_CODE_DIR)
        agent_options = _build_agent_options(
            agent_name,
            branch_name,
            config,
            kind,
            initial_message=initial_message,
            transfer_mode=TransferMode.GIT_WORKTREE,
        )
    else:
        source_location = HostLocation(host=config.source_host, path=config.source_dir)
        agent_options = _build_agent_options(agent_name, branch_name, config, kind, initial_message=initial_message)

    return api_create(
        source_location=source_location,
        target_host=target_host,
        agent_options=agent_options,
        mngr_ctx=mngr_ctx,
    )


def _launch_mapper(
    task: MapReduceTask,
    agent_name: AgentName,
    branch_name: str,
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    initial_message: str,
    existing_host: OnlineHostInterface | None = None,
    host_name: HostName | None = None,
) -> tuple[MapperInfo, OnlineHostInterface]:
    """Launch a single mapper agent for one task.

    ``agent_name``, ``branch_name``, and ``initial_message`` are passed in
    (rather than derived here) so the caller can build the prompt outside
    the worker thread and so the same identity can be reused when reporting
    a launch failure.
    """
    logger.info("Launching mapper '{}' for task: {}", agent_name, task.id)
    create_result = _create_agent(
        agent_name=agent_name,
        branch_name=branch_name,
        config=config,
        mngr_ctx=mngr_ctx,
        kind=AgentKind.MAPPER,
        initial_message=initial_message,
        existing_host=existing_host,
        host_name=host_name,
    )

    return (
        MapperInfo(
            task_id=task.id,
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            branch_name=branch_name,
            created_at=time.monotonic(),
        ),
        create_result.host,
    )


def _create_snapshot_host(
    recipe_name: str,
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    run_name: str,
) -> SnapshotName:
    """Launch a dedicated snapshotter agent, snapshot its host, then stop it."""
    agent_name = AgentName(f"{recipe_name}-{run_name}-snapshotter")
    host_name = HostName(f"{recipe_name}-{run_name}-snapshotter")

    logger.info("Launching snapshotter agent '{}' for provisioning...", agent_name)
    create_result = _create_agent(
        agent_name=agent_name,
        branch_name=f"{recipe_name}/{run_name}/snapshotter",
        config=config,
        mngr_ctx=mngr_ctx,
        kind=AgentKind.SNAPSHOTTER,
        host_name=host_name,
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
    except (MngrError, CleanupFailedGroup) as exc:
        logger.warning("Failed to stop agent '{}': {}", agent_name, exc)


def _create_host_pool(
    recipe_name: str,
    host_count: int,
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    run_name: str,
    max_parallel: int,
) -> list[OnlineHostInterface]:
    """Pre-create a pool of hosts for remote agent placement."""
    hosts: list[OnlineHostInterface] = []
    build = _resolve_build_options(config, mngr_ctx)

    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group,
        name="mapreduce_create_hosts",
        max_workers=max_parallel,
    ) as executor:
        futures = []
        host_environment = _build_host_environment(config)
        for i in range(host_count):
            h_name = HostName(f"{recipe_name}-{run_name}-host-{i}")
            new_host_opts = NewHostOptions(
                provider=config.provider_name,
                name=h_name,
                build=build,
                environment=host_environment,
            )
            futures.append(executor.submit(resolve_target_host, new_host_opts, mngr_ctx))
        for future in futures:
            try:
                hosts.append(future.result())
            except (MngrError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to create host: {}", exc)

    logger.info("Created {} host(s) for agent placement", len(hosts))
    return hosts


def launch_all_mappers(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    tasks: list[MapReduceTask],
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    launch_failures: list[AgentMetadata],
    run_name: str,
    max_parallel: int = 4,
    launch_delay_seconds: float = 2.0,
    agents_per_host: int = 4,
) -> tuple[list[MapperInfo], dict[str, OnlineHostInterface], SnapshotName | None]:
    """Launch a mapper agent for every task.

    For remote providers, agents_per_host controls how many agents share a single
    host. Hosts are pre-created in a pool and agents are assigned round-robin.
    For local providers, this setting is ignored (all agents share localhost).

    When the provider supports snapshots (e.g. modal) and the caller has not
    pre-supplied one via ``config.snapshot``, a snapshot host is built first
    and the resulting snapshot ID is propagated into ``launch_config`` so all
    other agents are launched from it. Providers without snapshot support
    (local, docker, ...) skip this step silently.

    Per-task launch failures are appended (in place) to ``launch_failures`` so
    they can be surfaced in the report. Mapper prompts are built on the calling
    thread via ``recipe.build_mapper_prompt(ctx, task)`` before the launch
    future is submitted, so the recipe doesn't need to be thread-safe.
    """
    agents: list[MapperInfo] = []
    agent_hosts: dict[str, OnlineHostInterface] = {}

    launch_config = config
    if config.snapshot is None:
        # Bootstrap any one-time backend resources (Modal's per-user environment)
        # before building the provider instance. The snapshotter and every test
        # agent that follows is a host creation, so this is the moment to allow
        # bootstrap; without it, snapshotting against a fresh Modal account
        # aborts with ProviderEmptyError before any host is created.
        bootstrap_backend_for_host_creation(config.provider_name, mngr_ctx)
        provider = get_provider_instance(config.provider_name, mngr_ctx)
        if provider.supports_snapshots:
            try:
                snapshot_name = _create_snapshot_host(recipe.name, config, mngr_ctx, run_name)
                launch_config = config.model_copy_update(to_update(config.field_ref().snapshot, snapshot_name))
            except (MngrError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to create snapshot, launching agents without snapshot: {}", exc)

    is_local = launch_config.provider_name.lower() == LOCAL_PROVIDER_NAME
    host_pool: list[OnlineHostInterface] = []
    if not is_local and agents_per_host > 0:
        host_count = math.ceil(len(tasks) / agents_per_host)
        if host_count > 0:
            host_pool = _create_host_pool(recipe.name, host_count, launch_config, mngr_ctx, run_name, max_parallel)

    used_suffixes: set[str] = set()
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group,
        name="mapreduce_launch",
        max_workers=max_parallel,
    ) as executor:
        futures: list[tuple[Future[tuple[MapperInfo, OnlineHostInterface]], MapReduceTask, AgentName, str]] = []
        for i, task in enumerate(tasks):
            if i > 0 and launch_delay_seconds > 0:
                time.sleep(launch_delay_seconds)
            existing_host = host_pool[i % len(host_pool)] if host_pool else None
            h_name = HostName(f"{recipe.name}-{run_name}-host-{i}") if not is_local and not host_pool else None
            agent_name, branch_name = _make_mapper_identity(recipe.name, run_name, task, used_suffixes)
            initial_message = recipe.build_mapper_prompt(ctx, task)
            futures.append(
                (
                    executor.submit(
                        _launch_mapper,
                        task,
                        agent_name,
                        branch_name,
                        launch_config,
                        mngr_ctx,
                        initial_message,
                        existing_host,
                        h_name,
                    ),
                    task,
                    agent_name,
                    branch_name,
                )
            )
        for future, task, agent_name, branch_name in futures:
            try:
                info, host = future.result()
                agents.append(info)
                agent_hosts[str(info.agent_id)] = host
            except (MngrError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to launch agent for {}: {}", task.id, exc)
                launch_failures.append(_make_launch_failure_metadata(task.id, agent_name, branch_name, exc))

    logger.info("Launched {} mapper agent(s)", len(agents))
    return agents, agent_hosts, launch_config.snapshot


def _launch_mapper_with_timeout(
    task: MapReduceTask,
    agent_name: AgentName,
    branch_name: str,
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    initial_message: str,
) -> tuple[MapperInfo, OnlineHostInterface]:
    """Launch a mapper agent with a timeout. Raises TimeoutError if creation takes too long."""
    with ConcurrencyGroupExecutor(mngr_ctx.concurrency_group, name="launch-mapper", max_workers=1) as executor:
        future = executor.submit(_launch_mapper, task, agent_name, branch_name, config, mngr_ctx, initial_message)
        return future.result(timeout=_AGENT_CREATION_TIMEOUT_SECONDS)


def launch_mappers_up_to_limit(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    remaining_tasks: list[MapReduceTask],
    pending_ids: set[str],
    max_agents: int,
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    all_agents: list[MapperInfo],
    all_hosts: dict[str, OnlineHostInterface],
    agent_id_to_info: dict[str, MapperInfo],
    launch_failures: list[AgentMetadata],
    run_name: str,
    used_suffixes: set[str],
) -> None:
    """Launch mappers from remaining_tasks until we hit max_agents running.

    Mutates remaining_tasks (pops from front), pending_ids, all_agents,
    all_hosts, agent_id_to_info, ``used_suffixes``, and launch_failures
    in place. Per-task launch failures are appended to ``launch_failures``
    so they can be surfaced in the report.
    """
    while remaining_tasks and (max_agents <= 0 or len(pending_ids) < max_agents):
        task = remaining_tasks.pop(0)
        agent_name, branch_name = _make_mapper_identity(recipe.name, run_name, task, used_suffixes)
        initial_message = recipe.build_mapper_prompt(ctx, task)
        try:
            info, host = _launch_mapper_with_timeout(task, agent_name, branch_name, config, mngr_ctx, initial_message)
        except TimeoutError:
            logger.warning("Agent creation timed out after {}s for {}", _AGENT_CREATION_TIMEOUT_SECONDS, task.id)
            launch_failures.append(
                _make_launch_failure_metadata(
                    task.id,
                    agent_name,
                    branch_name,
                    f"creation timed out after {_AGENT_CREATION_TIMEOUT_SECONDS}s",
                )
            )
            continue
        except (MngrError, OSError, BaseExceptionGroup) as exc:
            logger.warning("Failed to launch agent for {}: {}", task.id, exc)
            launch_failures.append(_make_launch_failure_metadata(task.id, agent_name, branch_name, exc))
            continue
        all_agents.append(info)
        all_hosts[str(info.agent_id)] = host
        agent_id_to_info[str(info.agent_id)] = info
        pending_ids.add(str(info.agent_id))


def launch_reducer_agent(
    recipe_name: str,
    prompt: str,
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    run_name: str,
    output_dir: Path,
) -> tuple[ReducerInfo, OnlineHostInterface]:
    """Launch a reducer agent that consumes the per-mapper output directories.

    Mappers always run with ``GIT_MIRROR`` transfer mode (see
    ``_build_agent_options``), so their branches never appear in the
    orchestrator's source repo automatically -- the only way the reducer
    gets at them is via the per-mapper ``branch.bundle`` files that the
    orchestrator has already pulled and extracted under ``output_dir``. The
    reducer host has no knowledge of those branches either, so we rsync
    ``output_dir`` into ``<work_dir>/REDUCER_INPUTS_DIRNAME/`` and deliver
    the reducer prompt via ``send_message``.
    """
    agent_name = AgentName(f"{recipe_name}-{run_name}-reducer")
    branch_name = f"{recipe_name}/{run_name}/reducer"
    host_name = HostName(f"{recipe_name}-{run_name}-reducer")

    logger.info("Launching reducer agent '{}'", agent_name)
    create_result = _create_agent(
        agent_name=agent_name,
        branch_name=branch_name,
        config=config,
        mngr_ctx=mngr_ctx,
        kind=AgentKind.REDUCER,
        initial_message=None,
        host_name=host_name,
    )

    destination = create_result.agent.work_dir / REDUCER_INPUTS_DIRNAME
    logger.info("Rsyncing reducer inputs to '{}:{}'", create_result.host.id, destination)
    # Trailing slash so rsync copies the *contents* of output_dir into the
    # reducer's inputs directory, not output_dir itself as a child.
    rsync_to_remote(
        local_path=f"{output_dir}/",
        remote_host=create_result.host,
        remote_path=destination,
        extra_args=(),
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=mngr_ctx.concurrency_group,
    )
    logger.info("Sending reducer prompt to '{}'", agent_name)
    require_interactive_agent(create_result.agent).send_message(prompt)

    return (
        ReducerInfo(
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            branch_name=branch_name,
        ),
        create_result.host,
    )
