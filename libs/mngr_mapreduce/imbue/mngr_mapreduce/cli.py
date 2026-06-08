"""CLI integration for the mngr-mapreduce framework.

Exposes the click-option decorator stack every map-reduce command should
add (``add_mapreduce_options``), the option-bag class that holds the
parsed values (``MapReduceCliOptions``), and the high-level entry points
(``run_mapreduce``, ``reintegrate_mapreduce``) that recipes invoke from
their thin click wrappers.
"""

import resource
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import TypeVar
from typing import assert_never

import click
from loguru import logger

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.cli.env_utils import resolve_env_vars
from imbue.mngr.cli.env_utils import resolve_labels
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UnknownBackendError
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.providers.registry import get_config_class
from imbue.mngr_mapreduce.agent_stopper import AgentStopper
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import LaunchConfig
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceRecipe
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.data_types import MapperInfo
from imbue.mngr_mapreduce.launching import launch_all_mappers
from imbue.mngr_mapreduce.launching import launch_reducer_agent
from imbue.mngr_mapreduce.mngr_cli import try_list_agents
from imbue.mngr_mapreduce.orchestration import launch_and_poll_mappers
from imbue.mngr_mapreduce.orchestration import wait_for_reducer
from imbue.mngr_mapreduce.pulling import pull_agent_outputs
from imbue.mngr_mapreduce.utils import get_base_commit
from imbue.mngr_mapreduce.utils import make_run_name

_DEFAULT_TIMEOUT_SECONDS = 3600.0
_DEFAULT_REDUCER_TIMEOUT_SECONDS = 3600.0

_MODAL_BACKEND_NAME = "modal"
_MIN_FD_LIMIT = 4096

# Server-side label that carries the framework-generated run name on every
# launched agent. Used by the reintegrate flow to discover prior agents.
RUN_NAME_LABEL_KEY = "mapreduce_run_name"

TDecorated = TypeVar("TDecorated", bound=Callable[..., Any])


class MapReduceCliOptions(CommonCliOptions):
    """Common options for any map-reduce command.

    Recipe-specific commands subclass this to add their own fields.
    """

    agent_type: str
    agent_template: tuple[str, ...]
    provider: str
    env: tuple[str, ...]
    label: tuple[str, ...]
    snapshot: str | None
    max_parallel_launch: int
    agents_per_host: int
    max_parallel_agents: int
    launch_delay: float
    poll_interval: float
    timeout: float
    reducer_timeout: float
    output_dir: str | None
    source: str | None
    reintegrate: bool
    run_name: str | None
    additional_authorized_keys: tuple[str, ...]


def add_mapreduce_options(command: TDecorated) -> TDecorated:
    """Decorator stack adding every framework-level option to a click command.

    Recipe-specific options should be added by additional ``@click.option``
    decorators stacked on top of this one (closer to the command function).
    """
    command = click.option(
        "--additional-authorized-host",
        "additional_authorized_keys",
        multiple=True,
        help="SSH public key line to install in authorized_keys on each agent host "
        "(mappers, reducer, host pool, and snapshotter), allowing inbound SSH [repeatable]",
    )(command)
    command = click.option(
        "--run-name",
        default=None,
        help="The run name. For new runs, overrides the auto-generated UTC YYYYMMDDHHMMSS timestamp; "
        "must not collide with prior runs whose agents are still discoverable. "
        "For --reintegrate, identifies which previous run to reintegrate (required).",
    )(command)
    command = click.option(
        "--reintegrate",
        is_flag=True,
        default=False,
        help="Re-read outcomes from a previous run, re-run the reducer, and regenerate the report. "
        "Skips discovery and mapper launching. The run to reintegrate is identified by --run-name.",
    )(command)
    command = click.option(
        "--source",
        default=None,
        type=click.Path(exists=True, file_okay=False),
        help="Source directory for discovery and agent work dirs [default: current directory]",
    )(command)
    command = click.option(
        "--output-dir",
        default=None,
        type=click.Path(),
        help="Directory for the run's outputs (HTML report at index.html, per-agent artifacts) "
        "[default: <recipe>_<timestamp>/]",
    )(command)
    command = click.option(
        "--reducer-timeout",
        default=_DEFAULT_REDUCER_TIMEOUT_SECONDS,
        show_default=True,
        type=float,
        help="Maximum seconds to wait for the reducer agent to finish",
    )(command)
    command = click.option(
        "--timeout",
        default=_DEFAULT_TIMEOUT_SECONDS,
        show_default=True,
        type=float,
        help="Maximum seconds each mapper can run before being stopped (per-agent timeout)",
    )(command)
    command = click.option(
        "--poll-interval",
        default=60.0,
        show_default=True,
        type=float,
        help="Seconds between polling cycles when waiting for agents to finish",
    )(command)
    command = click.option(
        "--launch-delay",
        default=2.0,
        show_default=True,
        type=float,
        help="Seconds to wait between launching each agent (avoids provider rate limits)",
    )(command)
    command = click.option(
        "--max-parallel-agents",
        default=0,
        show_default=True,
        type=int,
        help="Maximum number of mappers running at any one time (0 = no limit). "
        "When set, mappers are launched incrementally as earlier ones finish.",
    )(command)
    command = click.option(
        "--agents-per-host",
        default=4,
        show_default=True,
        type=int,
        help="Number of agents sharing each remote host (ignored for local provider)",
    )(command)
    command = click.option(
        "--max-parallel-launch",
        default=10,
        show_default=True,
        type=int,
        help="Maximum number of agents to launch concurrently (launch-time parallelism)",
    )(command)
    command = click.option(
        "--snapshot",
        default=None,
        help="Use an existing snapshot/image ID for all agents (skips building a fresh snapshot)",
    )(command)
    command = click.option(
        "--label",
        multiple=True,
        help="Agent label KEY=VALUE to attach to all launched agents [repeatable]",
    )(command)
    command = click.option(
        "--env",
        multiple=True,
        help="Environment variable KEY=VALUE to pass to agents [repeatable]",
    )(command)
    command = click.option(
        "--provider",
        default="local",
        show_default=True,
        help="Provider for agent hosts (e.g. local, docker, modal). Used for both mappers and the reducer.",
    )(command)
    command = click.option(
        "-t",
        "--agent-template",
        multiple=True,
        help="Create template to apply for mapper agents [repeatable, stacks in order]",
    )(command)
    command = click.option(
        "--agent-type",
        default="claude",
        show_default=True,
        help="Type of agent to launch for each task",
    )(command)
    return command


def disable_modal_initial_snapshot(mngr_ctx: MngrContext, provider_name: str) -> None:
    """Override the given modal-backed provider config to skip the per-agent initial snapshot.

    Modal's on_agent_created hook normally creates a 60-90s filesystem
    snapshot after each agent is created so the host can be restarted
    after a hard kill. The framework creates the snapshot it actually
    needs explicitly via ``provider.create_snapshot`` on the dedicated
    snapshotter, and every other host it creates is ephemeral, so the
    safety-net snapshot is dead weight that runs once *per agent*
    (multiplying the cost on pooled hosts). Disable it for the modal
    provider we're about to use, preserving the rest of the user's config.

    No-op when ``provider_name`` is not a modal-backed provider. Must be
    called before any ``get_provider_instance`` call for this name, since
    provider instances cache their config at construction.
    """
    instance_name = ProviderInstanceName(provider_name)
    existing = mngr_ctx.config.providers.get(instance_name)
    if existing is None:
        # Default-resolved provider: the instance name doubles as the
        # backend name. Only patch when that backend is modal.
        if provider_name != _MODAL_BACKEND_NAME:
            return
        try:
            config_class = get_config_class(ProviderBackendName(provider_name))
        except UnknownBackendError:
            return
        existing = config_class(backend=ProviderBackendName(provider_name))
    if str(existing.backend) != _MODAL_BACKEND_NAME:
        return
    mngr_ctx.config.providers[instance_name] = existing.model_copy_update(
        ("is_snapshotted_after_create", False),
    )


def raise_fd_limit() -> None:
    """Raise the soft file descriptor limit to handle many concurrent agents."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < _MIN_FD_LIMIT:
            new_soft = min(_MIN_FD_LIMIT, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
    except (ValueError, OSError):
        pass


def emit_task_count(count: int, output_opts: OutputOptions) -> None:
    """Emit the number of tasks discovered."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("tasks_discovered", {"count": count}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Discovered {} task(s)", count)
        case _ as unreachable:
            assert_never(unreachable)


def emit_agents_launched(count: int, output_opts: OutputOptions) -> None:
    """Emit the number of agents launched."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("agents_launched", {"count": count}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Launched {} agent(s)", count)
        case _ as unreachable:
            assert_never(unreachable)


def emit_report_path(path: Path, output_opts: OutputOptions) -> None:
    """Emit the path to the generated HTML report."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("report_generated", {"path": str(path)}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Report: {}", path)
        case _ as unreachable:
            assert_never(unreachable)


def build_launch_config(
    opts: MapReduceCliOptions,
    source_dir: Path,
    source_host: OnlineHostInterface,
    base_commit: str,
    run_name: str,
) -> LaunchConfig:
    """Assemble a ``LaunchConfig`` from CLI options.

    Stamps the run name into the label set so reintegrate can discover the
    agents later.
    """
    env_options = AgentEnvironmentOptions(env_vars=resolve_env_vars((), opts.env))
    label_options = resolve_labels(opts.label)
    run_labels = dict(label_options.labels)
    run_labels[RUN_NAME_LABEL_KEY] = run_name
    label_options = AgentLabelOptions(labels=run_labels)
    return LaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        base_commit=base_commit,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.provider),
        env_options=env_options,
        label_options=label_options,
        snapshot=SnapshotName(opts.snapshot) if opts.snapshot is not None else None,
        templates=opts.agent_template,
        additional_authorized_keys=opts.additional_authorized_keys,
    )


def _run_reducer_phase(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    opts: MapReduceCliOptions,
    mngr_ctx: MngrContext,
    config: LaunchConfig,
    mapper_metadata: list[AgentMetadata],
    stopper: AgentStopper,
) -> AgentMetadata | None:
    """Launch the reducer agent if at least one mapper produced outputs.

    Returns the reducer's ``AgentMetadata``, or ``None`` when no mapper
    succeeded (the reducer would have an empty inputs dir and nothing to
    do). The recipe owns interpretation of the reducer's outputs via its
    ``on_reducer_finalized`` hook.
    """
    has_any_successful_mapper = any(
        meta.kind is AgentKind.MAPPER and meta.error_summary is None for meta in mapper_metadata
    )
    if not has_any_successful_mapper:
        return None

    prompt = recipe.build_reducer_prompt(ctx)
    try:
        info, host = launch_reducer_agent(
            recipe_name=recipe.name,
            prompt=prompt,
            config=config,
            mngr_ctx=mngr_ctx,
            run_name=ctx.run_name,
            output_dir=ctx.output_dir,
        )
    except (MngrError, OSError, BaseExceptionGroup) as exc:
        logger.warning("Failed to launch reducer agent: {}", exc)
        return None

    deadline = time.monotonic() + opts.reducer_timeout
    return wait_for_reducer(
        recipe=recipe,
        ctx=ctx,
        info=info,
        host=host,
        provider_name=config.provider_name,
        mngr_ctx=mngr_ctx,
        poll_interval_seconds=opts.poll_interval,
        deadline=deadline,
        stopper=stopper,
    )


def run_mapreduce(
    recipe: MapReduceRecipe,
    opts: MapReduceCliOptions,
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
) -> None:
    """End-to-end entry point: discover, map, reduce, report.

    Recipes call this from their thin click wrapper after building the
    recipe instance from recipe-specific CLI flags. The framework handles
    everything else (run-name generation, launching, polling, reducing,
    reporting, S3 upload).

    On ``opts.reintegrate``, ``reintegrate_mapreduce`` is invoked instead;
    the original mapper launch is skipped.
    """
    raise_fd_limit()
    disable_modal_initial_snapshot(mngr_ctx, opts.provider)

    source_dir = Path(opts.source) if opts.source is not None else Path.cwd()

    # The AgentStopper owns background threads for fire-and-forget post-finalize
    # stop_agents calls; see agent_stopper.py for the why.
    if opts.reintegrate:
        with AgentStopper() as stopper:
            reintegrate_mapreduce(recipe, opts, mngr_ctx, output_opts, source_dir, stopper)
        return

    run_name = opts.run_name if opts.run_name else make_run_name()
    base_commit = get_base_commit(source_dir, mngr_ctx.concurrency_group)
    source_host = get_local_host(mngr_ctx)

    output_dir = Path(opts.output_dir) if opts.output_dir is not None else Path(f"{recipe.name}_{run_name}")
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx = MapReduceContext(
        mngr_ctx=mngr_ctx,
        source_dir=source_dir,
        run_name=run_name,
        output_dir=output_dir,
        output_opts=output_opts,
    )

    tasks = recipe.discover(ctx)
    emit_task_count(len(tasks), output_opts)

    config = build_launch_config(
        opts=opts,
        source_dir=source_dir,
        source_host=source_host,
        base_commit=base_commit,
        run_name=run_name,
    )

    try:
        with AgentStopper() as stopper:
            _run_pipeline(recipe, ctx, opts, mngr_ctx, output_opts, config, tasks, stopper)
    except KeyboardInterrupt:
        traceback.print_exc()
        raise


def _run_pipeline(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    opts: MapReduceCliOptions,
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    config: LaunchConfig,
    tasks: list[MapReduceTask],
    stopper: AgentStopper,
) -> None:
    """Launch + poll + reduce + report.

    When ``max_parallel_agents > 0`` and fewer mappers can run concurrently
    than there are tasks, mappers launch incrementally as earlier ones
    finish (the "batched" mode); otherwise every mapper launches up front
    and polling waits for them all together.
    """
    use_batched = opts.max_parallel_agents > 0 and opts.max_parallel_agents < len(tasks)

    launch_failures: list[AgentMetadata] = []

    snapshot_name: SnapshotName | None = config.snapshot
    if use_batched:
        # The batched path does not snapshot -- each agent is launched on
        # demand, so there's no "build one first, snapshot, launch the
        # rest" phase to hook into.
        agent_infos: list[MapperInfo] = []
        agent_hosts: dict[str, OnlineHostInterface] = {}
        remaining_tasks = tasks
    else:
        agent_infos, agent_hosts, snapshot_name = launch_all_mappers(
            recipe=recipe,
            ctx=ctx,
            tasks=tasks,
            config=config,
            mngr_ctx=mngr_ctx,
            launch_failures=launch_failures,
            run_name=ctx.run_name,
            max_parallel=opts.max_parallel_launch,
            launch_delay_seconds=opts.launch_delay,
            agents_per_host=opts.agents_per_host,
        )
        emit_agents_launched(len(agent_infos), output_opts)
        remaining_tasks = []

    mapper_metadata = launch_and_poll_mappers(
        recipe=recipe,
        ctx=ctx,
        tasks=remaining_tasks,
        config=config,
        mngr_ctx=mngr_ctx,
        max_agents=opts.max_parallel_agents,
        agent_timeout_seconds=opts.timeout,
        poll_interval_seconds=opts.poll_interval,
        all_agents=agent_infos,
        all_hosts=agent_hosts,
        launch_failures=launch_failures,
        stopper=stopper,
    )

    if use_batched:
        emit_agents_launched(len(agent_infos), output_opts)

    # The reducer runs on the same provider as the mappers and reuses any
    # snapshot built for them.
    reducer_config = config.model_copy_update(to_update(config.field_ref().snapshot, snapshot_name))
    reducer_meta = _run_reducer_phase(recipe, ctx, opts, mngr_ctx, reducer_config, mapper_metadata, stopper)

    final_path = _render_final_report(recipe, ctx, mapper_metadata, reducer_meta)
    if final_path is not None:
        emit_report_path(final_path, output_opts)

    # If no mapper ever launched successfully, surface a non-zero exit so
    # CI callers don't read the run as successful.
    if tasks and not agent_infos:
        raise MngrError(
            f"All {len(launch_failures)} mapper launches failed; see the HTML report for per-agent error summaries."
        )


def _render_final_report(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    mapper_metadata: list[AgentMetadata],
    reducer_meta: AgentMetadata | None,
) -> Path | None:
    """Render the final post-reduce report.

    Wrapper around ``recipe.render_report`` that swallows recipe errors --
    a broken renderer shouldn't sink an otherwise-successful run.
    """
    try:
        return recipe.render_report(ctx, mapper_metadata, reducer_meta)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Recipe render_report raised at end of run: {}", exc)
        return None


def reintegrate_mapreduce(
    recipe: MapReduceRecipe,
    opts: MapReduceCliOptions,
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    source_dir: Path,
    stopper: AgentStopper,
) -> None:
    """Re-read outcomes from a previous map-reduce run and re-run the reducer.

    Discovers prior agents by the ``mapreduce_run_name`` label, pulls each
    one's outputs into the output dir, re-fires ``on_mapper_finalized`` for
    each, then runs the reducer just like a normal run.
    """
    if not opts.run_name:
        raise click.UsageError("--reintegrate requires --run-name <NAME> (the run name to reintegrate).")
    run_name = opts.run_name
    is_human = output_opts.output_format == OutputFormat.HUMAN
    if is_human:
        write_human_line("Reintegrating run: {}", run_name)

    list_result = try_list_agents(mngr_ctx)
    if list_result is None:
        raise MngrError("Failed to list agents. Cannot reintegrate.")
    matching = [
        detail
        for detail in list_result.agents
        if detail.labels.get(RUN_NAME_LABEL_KEY) == run_name
        and detail.labels.get("mapreduce_role") != AgentKind.REDUCER.value
    ]
    if is_human:
        write_human_line("Found {} agent(s) from run {}", len(matching), run_name)

    if not matching:
        raise click.UsageError(f"No agents found for run name {run_name!r}. Nothing to reintegrate.")

    source_host = get_local_host(mngr_ctx)

    output_dir = (
        Path(opts.output_dir) if opts.output_dir is not None else Path(f"{recipe.name}_{run_name}_reintegrate")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx = MapReduceContext(
        mngr_ctx=mngr_ctx,
        source_dir=source_dir,
        run_name=run_name,
        output_dir=output_dir,
        output_opts=output_opts,
    )

    # Build mapper metadata + pull each agent's outputs. Re-fire the recipe's
    # on_mapper_finalized for every successful pull so the recipe can re-apply
    # whatever it does (e.g. branch bundles).
    mapper_metadata: list[AgentMetadata] = []
    for detail in matching:
        task_id = detail.labels.get("mapreduce_task_id", str(detail.name))
        meta = AgentMetadata(
            kind=AgentKind.MAPPER,
            agent_name=detail.name,
            task_id=task_id,
            branch_name=detail.initial_branch,
        )
        local_dest = pull_agent_outputs(
            mngr_ctx=mngr_ctx,
            provider_name=detail.host.provider_name,
            host_id=detail.host.id,
            agent_id=detail.id,
            agent_name=detail.name,
            destination_dir=output_dir,
        )
        if local_dest is not None:
            info = MapperInfo(
                task_id=task_id,
                agent_id=detail.id,
                agent_name=detail.name,
                branch_name=detail.initial_branch or "",
                created_at=0.0,
            )
            try:
                recipe.on_mapper_finalized(ctx, local_dest, info)
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Recipe on_mapper_finalized raised during reintegrate: {}", exc)
        else:
            meta = AgentMetadata(
                kind=AgentKind.MAPPER,
                agent_name=detail.name,
                task_id=task_id,
                branch_name=detail.initial_branch,
                error_summary="Could not pull outputs during reintegrate (host unreachable?).",
            )
        mapper_metadata.append(meta)

    base_commit = get_base_commit(source_dir, mngr_ctx.concurrency_group)

    # Pre-reduce render so the recipe can mirror/emit; framework discards path.
    _render_final_report(recipe, ctx, mapper_metadata, None)

    config = build_launch_config(
        opts=opts,
        source_dir=source_dir,
        source_host=source_host,
        base_commit=base_commit,
        run_name=run_name,
    )
    reducer_meta = _run_reducer_phase(recipe, ctx, opts, mngr_ctx, config, mapper_metadata, stopper)

    final_path = _render_final_report(recipe, ctx, mapper_metadata, reducer_meta)
    if final_path is not None:
        emit_report_path(final_path, output_opts)
