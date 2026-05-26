"""CLI command for test-mapreduce."""

import resource
import time
import traceback
from pathlib import Path
from typing import assert_never

import click
from loguru import logger

from imbue.mngr.cli.common_opts import CommonCliOptions
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.env_utils import resolve_env_vars
from imbue.mngr.cli.env_utils import resolve_labels
from imbue.mngr.cli.headless_runner import get_local_host
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UnknownBackendError
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.providers.registry import get_config_class
from imbue.mngr_tmr.data_types import AgentKind
from imbue.mngr_tmr.data_types import AgentMetadata
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.launching import launch_all_test_agents
from imbue.mngr_tmr.launching import launch_integrator_agent
from imbue.mngr_tmr.mngr_cli import try_list_agents
from imbue.mngr_tmr.orchestration import launch_and_poll_agents
from imbue.mngr_tmr.orchestration import wait_for_integrator
from imbue.mngr_tmr.pulling import pull_agent_branch
from imbue.mngr_tmr.pulling import pull_agent_outputs
from imbue.mngr_tmr.pulling import pull_integrator_outputs
from imbue.mngr_tmr.report import generate_html_report
from imbue.mngr_tmr.report import list_pullable_branches
from imbue.mngr_tmr.report_upload import maybe_upload_report
from imbue.mngr_tmr.utils import collect_tests
from imbue.mngr_tmr.utils import get_base_commit
from imbue.mngr_tmr.utils import make_run_name

_DEFAULT_TIMEOUT_SECONDS = 3600.0
_DEFAULT_INTEGRATOR_TIMEOUT_SECONDS = 3600.0

_MODAL_BACKEND_NAME = "modal"


def _disable_modal_initial_snapshot(mngr_ctx: MngrContext, provider_names: tuple[str, ...]) -> None:
    """Override modal-backed provider configs to skip the per-agent initial snapshot.

    Modal's on_agent_created hook normally creates a 60-90s filesystem
    snapshot after each agent is created so the host can be restarted
    after a hard kill. TMR creates the snapshot it actually needs
    explicitly via ``provider.create_snapshot`` on the dedicated
    snapshotter, and every other host TMR creates is ephemeral, so the
    safety-net snapshot is dead weight that runs once *per agent*
    (multiplying the cost on pooled hosts). Disable it for any modal
    provider TMR is about to use, preserving the rest of the user's
    config.

    Must be called before any ``get_provider_instance`` call for these
    names, since provider instances cache their config at construction.
    """
    seen: set[ProviderInstanceName] = set()
    for raw_name in provider_names:
        instance_name = ProviderInstanceName(raw_name)
        if instance_name in seen:
            continue
        seen.add(instance_name)
        existing = mngr_ctx.config.providers.get(instance_name)
        if existing is None:
            # Default-resolved provider: the instance name doubles as the
            # backend name. Only patch when that backend is modal.
            if raw_name != _MODAL_BACKEND_NAME:
                continue
            try:
                config_class = get_config_class(ProviderBackendName(raw_name))
            except UnknownBackendError:
                continue
            existing = config_class(backend=ProviderBackendName(raw_name))
        if str(existing.backend) != _MODAL_BACKEND_NAME:
            continue
        mngr_ctx.config.providers[instance_name] = existing.model_copy_update(
            ("is_snapshotted_after_create", False),
        )


class TmrCliOptions(CommonCliOptions):
    """Options passed from the CLI to the tmr command."""

    pytest_args: tuple[str, ...]
    testing_flags: tuple[str, ...]
    agent_type: str
    integrator_type: str | None
    agent_template: tuple[str, ...]
    integrator_template: tuple[str, ...]
    provider: str
    integrator_provider: str
    env: tuple[str, ...]
    label: tuple[str, ...]
    prompt_suffix: str | None
    use_snapshot: bool
    snapshot: str | None
    max_parallel_launch: int
    agents_per_host: int
    max_parallel_agents: int
    launch_delay: float
    poll_interval: float
    timeout: float
    integrator_timeout: float
    output_dir: str | None
    source: str | None
    reintegrate: bool
    run_name: str | None
    additional_authorized_keys: tuple[str, ...]


_MIN_FD_LIMIT = 4096


def _raise_fd_limit() -> None:
    """Raise the soft file descriptor limit to handle many concurrent agents."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < _MIN_FD_LIMIT:
            new_soft = min(_MIN_FD_LIMIT, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
    except (ValueError, OSError):
        pass


class _TmrCommand(click.Command):
    """Custom Command that handles -- separator for testing flags.

    Everything before -- is treated as positional args (test paths/patterns).
    Everything after -- is captured as testing_flags and shared between
    pytest discovery and individual test runs.

    This is the same trick used by _CreateCommand in the mngr create CLI.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--" in args:
            idx = args.index("--")
            after_dash = tuple(args[idx + 1 :])
            args = args[:idx]
        else:
            after_dash = ()
        result = super().parse_args(ctx, args)
        ctx.params["testing_flags"] = after_dash
        return result


def _emit_test_count(count: int, output_opts: OutputOptions) -> None:
    """Emit the number of tests collected."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("tests_collected", {"count": count}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Collected {} test(s)", count)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_agents_launched(count: int, output_opts: OutputOptions) -> None:
    """Emit the number of agents launched."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("agents_launched", {"count": count}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Launched {} agent(s)", count)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_report_path(path: Path, output_opts: OutputOptions) -> None:
    """Emit the path to the generated HTML report."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("report_generated", {"path": str(path)}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Report: {}", path)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_report_url(url: str | None, output_opts: OutputOptions) -> None:
    """Emit the public URL of the report mirror, if upload occurred."""
    if url is None:
        return
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("report_url", {"url": url}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Report URL: {}", url)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_integrator_branch(branch_name: str | None, output_opts: OutputOptions) -> None:
    """Emit the name of the integrator branch, if one was produced."""
    if branch_name is None:
        return
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("integrator_branch", {"branch_name": branch_name}, output_opts.output_format)
        case OutputFormat.HUMAN:
            pass
        case _ as unreachable:
            assert_never(unreachable)


def _run_reintegrate(
    opts: TmrCliOptions,
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    source_dir: Path,
) -> None:
    """Re-read outcomes from a previous TMR run and re-run integration.

    Discovers agents by the tmr_run_name label, reads their result files,
    re-runs the integrator, and generates a fresh report.
    """
    assert opts.reintegrate
    if not opts.run_name:
        raise click.UsageError("--reintegrate requires --run-name <NAME> (the run name to reintegrate).")
    run_name = opts.run_name
    is_human = output_opts.output_format == OutputFormat.HUMAN
    if is_human:
        write_human_line("Reintegrating run: {}", run_name)

    # Discover agents from the previous run by label
    list_result = try_list_agents(mngr_ctx)
    if list_result is None:
        raise MngrError("Failed to list agents. Cannot reintegrate.")
    matching_agents = [
        detail
        for detail in list_result.agents
        if detail.labels.get("tmr_run_name") == run_name
        and detail.labels.get("tmr_role") != AgentKind.INTEGRATOR.value
    ]
    if is_human:
        write_human_line("Found {} agent(s) from run {}", len(matching_agents), run_name)

    if not matching_agents:
        raise click.UsageError(f"No agents found for run name {run_name!r}. Nothing to reintegrate.")

    # Get local host (needed by the integrator config built later).
    source_host = get_local_host(mngr_ctx)

    # Build per-agent metadata from discovered agents. Output pulling uses
    # the volume API directly, so the testing agents' hosts do not need to
    # be online.
    test_agent_metadata: list[AgentMetadata] = [
        AgentMetadata(
            kind=AgentKind.TESTING_AGENT,
            agent_name=detail.name,
            test_node_id=detail.labels.get("test_node_id", str(detail.name)),
            branch_name=detail.initial_branch,
        )
        for detail in matching_agents
    ]

    # Compute output directory
    output_dir = Path(opts.output_dir) if opts.output_dir is not None else Path(f"tmr_{run_name}_reintegrate")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pull outputs (artifacts + outcome + branch bundle) for each agent via
    # the volume API. The host does not need to be online for this step.
    cg = mngr_ctx.concurrency_group
    for detail in matching_agents:
        pull_agent_outputs(
            mngr_ctx=mngr_ctx,
            provider_name=detail.host.provider_name,
            host_id=detail.host.id,
            agent_id=detail.id,
            agent_name=detail.name,
            branch_name=detail.initial_branch,
            destination_dir=output_dir,
            source_dir=source_dir,
            cg=cg,
        )

    base_commit = get_base_commit(source_dir, cg)

    # Write pre-integrator report
    report_path = generate_html_report(
        test_agent_metadata,
        output_dir,
        run_commands=_build_run_commands(run_name),
    )
    _emit_report_url(maybe_upload_report(report_path, run_name), output_opts)

    # Run integrator (carry the same tmr_run_name so it shows up in this run's
    # agent list; the tmr_role label is set automatically by _create_tmr_agent
    # from AgentKind.INTEGRATOR, which the reintegrate filter above looks for).
    env_options = AgentEnvironmentOptions(env_vars=resolve_env_vars((), opts.env))
    run_labels = dict(resolve_labels(opts.label).labels)
    run_labels["tmr_run_name"] = run_name
    label_options = AgentLabelOptions(labels=run_labels)
    integrator_agent_type = opts.integrator_type if opts.integrator_type is not None else opts.agent_type
    integrator_templates = opts.integrator_template if opts.integrator_template else opts.agent_template
    integrator_config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        base_commit=base_commit,
        agent_type=AgentTypeName(integrator_agent_type),
        provider_name=ProviderInstanceName(opts.integrator_provider),
        env_options=env_options,
        label_options=label_options,
        templates=integrator_templates,
        additional_authorized_keys=opts.additional_authorized_keys,
    )
    integrator_meta = _run_integrator_phase(
        test_agent_metadata, integrator_config, mngr_ctx, opts, output_dir, run_name, base_commit=base_commit
    )
    integrated_branch = integrator_meta.branch_name if integrator_meta is not None else None
    report_path = generate_html_report(
        test_agent_metadata,
        output_dir,
        integrator_metadata=integrator_meta,
        run_commands=_build_run_commands(run_name, integrated_branch),
    )
    _emit_report_path(output_dir / "index.html", output_opts)
    _emit_report_url(maybe_upload_report(report_path, run_name), output_opts)
    _emit_integrator_branch(integrated_branch, output_opts)
    _print_run_commands(run_name, output_opts, integrated_branch)


def _run_integrator_phase(
    test_agent_metadata: list[AgentMetadata],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    opts: TmrCliOptions,
    output_dir: Path,
    run_name: str,
    base_commit: str | None = None,
) -> AgentMetadata | None:
    """Launch an integrator agent to cherry-pick all fix branches into a linear stack.

    All pullable branches are integrated. Test/doc/tutorial commits are squashed
    into one commit; FIX_IMPL commits are kept separate and stacked by priority.
    Returns the integrator's metadata (kind=INTEGRATOR) with branch_name set
    when a branch was produced; the report reads the integrator outcome JSON
    from disk.
    """
    fix_branches = list_pullable_branches(test_agent_metadata, output_dir)
    if not fix_branches:
        return None

    try:
        integrator, integrator_host = launch_integrator_agent(
            fix_branches=fix_branches,
            config=config,
            mngr_ctx=mngr_ctx,
            run_name=run_name,
        )
    except (MngrError, HostError, AgentError, OSError, BaseExceptionGroup) as exc:
        logger.warning("Failed to launch integrator agent: {}", exc)
        return None

    integrator_deadline = time.monotonic() + opts.integrator_timeout
    integrator_branch = wait_for_integrator(
        integrator=integrator,
        poll_interval_seconds=opts.poll_interval,
        host=integrator_host,
        deadline=integrator_deadline,
    )

    if integrator_branch is None:
        return AgentMetadata(
            kind=AgentKind.INTEGRATOR,
            agent_name=integrator.agent_name,
            branch_name=None,
            error_summary="Integrator timed out before producing a branch.",
        )

    # Pull the integrator's outputs into output_dir so the reporter can parse
    # the integrator outcome JSON from disk.
    pull_integrator_outputs(
        integrator.agent_id,
        integrator.agent_name,
        integrator_host,
        output_dir,
        mngr_ctx.concurrency_group,
    )

    # Only pull branches from remote providers; local worktree branches already exist
    is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
    if is_remote:
        pull_agent_branch(
            integrator.agent_id,
            integrator.agent_name,
            integrator_branch,
            integrator_host,
            config.source_dir,
            mngr_ctx.concurrency_group,
            base_commit=base_commit,
        )

    return AgentMetadata(
        kind=AgentKind.INTEGRATOR,
        agent_name=integrator.agent_name,
        branch_name=integrator_branch,
    )


@click.command("tmr", cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--agent-type",
    default="claude",
    show_default=True,
    help="Type of agent to launch for each test",
)
@click.option(
    "--integrator-type",
    default=None,
    help="Type of agent for the integrator (defaults to --agent-type)",
)
@click.option(
    "-t",
    "--agent-template",
    multiple=True,
    help="Create template to apply for testing agents [repeatable, stacks in order]",
)
@click.option(
    "--integrator-template",
    multiple=True,
    default=None,
    help="Create template to apply for the integrator agent (defaults to --agent-template)",
)
@click.option(
    "--provider",
    default="local",
    show_default=True,
    help="Provider for agent hosts (e.g. local, docker, modal)",
)
@click.option(
    "--integrator-provider",
    default="local",
    show_default=True,
    help="Provider for the integrator agent (defaults to local since there is only one)",
)
@click.option(
    "--env",
    multiple=True,
    help="Environment variable KEY=VALUE to pass to agents [repeatable]",
)
@click.option(
    "--label",
    multiple=True,
    help="Agent label KEY=VALUE to attach to all launched agents [repeatable]",
)
@click.option(
    "--prompt-suffix",
    default=None,
    help="Additional text to append to the agent prompt",
)
@click.option(
    "--use-snapshot",
    is_flag=True,
    default=False,
    help="Build one agent first, snapshot its host, then launch remaining agents from the snapshot (faster for remote providers)",
)
@click.option(
    "--snapshot",
    default=None,
    help="Use an existing snapshot/image ID for all agents (skips building; implies --use-snapshot behavior)",
)
@click.option(
    "--max-parallel-launch",
    default=10,
    show_default=True,
    type=int,
    help="Maximum number of agents to launch concurrently (launch-time parallelism)",
)
@click.option(
    "--agents-per-host",
    default=4,
    show_default=True,
    type=int,
    help="Number of agents sharing each remote host (ignored for local provider)",
)
@click.option(
    "--max-parallel-agents",
    default=0,
    show_default=True,
    type=int,
    help="Maximum number of agents running at any one time (0 = no limit). "
    "When set, agents are launched incrementally as earlier ones finish.",
)
@click.option(
    "--launch-delay",
    default=2.0,
    show_default=True,
    type=float,
    help="Seconds to wait between launching each agent (avoids provider rate limits)",
)
@click.option(
    "--poll-interval",
    default=60.0,
    show_default=True,
    type=float,
    help="Seconds between polling cycles when waiting for agents to finish",
)
@click.option(
    "--timeout",
    default=_DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    type=float,
    help="Maximum seconds each agent can run before being stopped (per-agent timeout)",
)
@click.option(
    "--integrator-timeout",
    default=_DEFAULT_INTEGRATOR_TIMEOUT_SECONDS,
    show_default=True,
    type=float,
    help="Maximum seconds to wait for the integrator agent to merge fix branches",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(),
    help="Directory for the run's outputs (HTML report at index.html, per-agent artifacts) "
    "[default: tmr_<timestamp>/]",
)
@click.option(
    "--source",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Source directory for test collection and agent work dirs [default: current directory]",
)
@click.option(
    "--reintegrate",
    is_flag=True,
    default=False,
    help="Re-read outcomes from a previous TMR run, re-run the integrator, and regenerate the report. "
    "Skips test collection and agent launching. The run to reintegrate is identified by --run-name.",
)
@click.option(
    "--run-name",
    default=None,
    help="The run name. For new runs, overrides the auto-generated UTC YYYYMMDDHHMMSS timestamp; "
    "must not collide with prior runs whose agents are still discoverable, or agent creation will fail. "
    "For --reintegrate, identifies which previous run to reintegrate (required).",
)
@click.option(
    "--additional-authorized-host",
    "additional_authorized_keys",
    multiple=True,
    help="SSH public key line to install in authorized_keys on each agent host "
    "(test agents, integrator, host pool, and snapshotter), allowing inbound SSH [repeatable]",
)
@add_common_options
@click.pass_context
def tmr(ctx: click.Context, **kwargs: object) -> None:
    try:
        _tmr_body(ctx)
    except click.UsageError as exc:
        # click.UsageError exits 2 by default; convert to a plain ClickException
        # with exit_code=1 so the tmr convention (1=usage, 2=everything else)
        # is preserved.
        wrapped = click.ClickException(exc.format_message())
        wrapped.exit_code = 1
        raise wrapped from exc
    except click.ClickException as exc:
        # MngrError (and other ClickException subclasses) default to exit 1.
        # Promote to exit 2 per the tmr convention.
        exc.exit_code = 2
        raise


def _tmr_body(ctx: click.Context) -> None:
    """Run the tmr command body. Wrapped by ``tmr`` so exit codes follow the
    1=usage / 2=other convention regardless of which layer raised."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="tmr",
        command_class=TmrCliOptions,
    )

    # Raise the soft FD limit to handle many concurrent agents.
    # Each agent process (tmux + claude) opens many files, and list_agents
    # enumerates all hosts which can push the system near the FD limit.
    _raise_fd_limit()

    _disable_modal_initial_snapshot(mngr_ctx, (opts.provider, opts.integrator_provider))

    source_dir = Path(opts.source) if opts.source is not None else Path.cwd()

    if opts.reintegrate:
        _run_reintegrate(opts, mngr_ctx, output_opts, source_dir)
        return

    testing_flags = opts.testing_flags

    # Step 1: Remember the base commit so we can create local branches for remote agents
    base_commit = get_base_commit(source_dir, mngr_ctx.concurrency_group)

    # Step 2: Collect tests (positional paths + testing flags go to discovery)
    test_node_ids = collect_tests(
        pytest_args=opts.pytest_args + testing_flags,
        source_dir=source_dir,
        cg=mngr_ctx.concurrency_group,
    )
    _emit_test_count(len(test_node_ids), output_opts)

    # Step 3: Get the local host for source_location (tests are collected locally)
    source_host = get_local_host(mngr_ctx)

    # Step 4: Build launch config and launch agents
    env_options = AgentEnvironmentOptions(env_vars=resolve_env_vars((), opts.env))
    label_options = resolve_labels(opts.label)
    provided_snapshot = SnapshotName(opts.snapshot) if opts.snapshot is not None else None

    # Step 5: Generate the shared run name (YYYYMMDDHHMMSS, UTC) -- or accept
    # an explicit override via --run-name. Used as the discriminator in agent
    # / host / branch names, the output directory, the tmr_run_name label, and
    # the e2e run-name flag (with a 'tmr_' prefix there to give
    # .test_output/e2e/tmr_{run}_try_N/ provenance vs. ad-hoc local pytest
    # runs). Agents append _try_1, _try_2 etc. for each test run.
    run = opts.run_name if opts.run_name else make_run_name()
    testing_flags = testing_flags + ("--mngr-e2e-run-name", f"tmr_{run}")

    # Add tmr_run_name so reintegrate can find this run's agents. The
    # tmr_role label is set automatically by _create_tmr_agent based on
    # the AgentKind passed at each launch site (so the reintegrate filter
    # can exclude AgentKind.INTEGRATOR.value).
    run_labels = dict(label_options.labels)
    run_labels["tmr_run_name"] = run
    label_options = AgentLabelOptions(labels=run_labels)

    config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        base_commit=base_commit,
        agent_type=AgentTypeName(opts.agent_type),
        provider_name=ProviderInstanceName(opts.provider),
        env_options=env_options,
        label_options=label_options,
        snapshot=provided_snapshot,
        templates=opts.agent_template,
        additional_authorized_keys=opts.additional_authorized_keys,
    )

    try:
        _run_tmr_pipeline(
            opts,
            mngr_ctx,
            output_opts,
            source_dir,
            config,
            testing_flags,
            run,
            base_commit,
            source_host,
            label_options,
            test_node_ids,
            provided_snapshot,
            env_options,
        )
    except KeyboardInterrupt:
        traceback.print_exc()
        _print_run_commands(run, output_opts, None)
        raise


def _run_tmr_pipeline(
    opts: TmrCliOptions,
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    source_dir: Path,
    config: TmrLaunchConfig,
    testing_flags: tuple[str, ...],
    run: str,
    base_commit: str,
    source_host: OnlineHostInterface,
    label_options: AgentLabelOptions,
    test_node_ids: list[str],
    provided_snapshot: SnapshotName | None,
    env_options: AgentEnvironmentOptions,
) -> None:
    """Run the main TMR pipeline (launch, poll, integrate, report)."""
    # Step 6: Compute output directory before launching
    output_dir = Path(opts.output_dir) if opts.output_dir is not None else Path(f"tmr_{run}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 7: Launch and poll agents
    # When max_parallel_agents > 0, agents are launched incrementally as earlier ones finish.
    # Otherwise, all agents are launched up front and then polled via the same function.
    use_batched = opts.max_parallel_agents > 0 and opts.max_parallel_agents < len(test_node_ids)

    launch_failures: list[AgentMetadata] = []

    if use_batched:
        if opts.use_snapshot and output_opts.output_format == OutputFormat.HUMAN:
            write_human_line("WARNING: --use-snapshot is not supported with --max-parallel-agents and will be ignored")
        agent_infos: list[TestAgentInfo] = []
        agent_hosts: dict[str, OnlineHostInterface] = {}
        remaining_node_ids = test_node_ids
    else:
        # When --snapshot is provided, all agents use it directly (no need for --use-snapshot)
        agent_infos, agent_hosts, _snapshot_name = launch_all_test_agents(
            test_node_ids=test_node_ids,
            config=config,
            mngr_ctx=mngr_ctx,
            pytest_flags=testing_flags,
            launch_failures=launch_failures,
            run_name=run,
            prompt_suffix=opts.prompt_suffix or "",
            use_snapshot=opts.use_snapshot and provided_snapshot is None,
            max_parallel=opts.max_parallel_launch,
            launch_delay_seconds=opts.launch_delay,
            agents_per_host=opts.agents_per_host,
        )
        _emit_agents_launched(len(agent_infos), output_opts)
        remaining_node_ids = []

    test_agent_metadata = launch_and_poll_agents(
        test_node_ids=remaining_node_ids,
        config=config,
        mngr_ctx=mngr_ctx,
        pytest_flags=testing_flags,
        prompt_suffix=opts.prompt_suffix or "",
        max_agents=opts.max_parallel_agents,
        agent_timeout_seconds=opts.timeout,
        poll_interval_seconds=opts.poll_interval,
        output_dir=output_dir,
        all_agents=agent_infos,
        all_hosts=agent_hosts,
        launch_failures=launch_failures,
        run_name=run,
        source_dir=source_dir,
    )

    if use_batched:
        _emit_agents_launched(len(agent_infos), output_opts)

    # Step 8: Write the post-polling report (pre-integrator). Artifacts and
    # branch bundles were already downloaded during per-agent finalization;
    # the reporter parses outcome JSON from disk.
    report_path = generate_html_report(test_agent_metadata, output_dir)
    _emit_report_url(maybe_upload_report(report_path, run), output_opts)

    # Step 9: Build integrator config (defaults to local provider) and integrate.
    # The integrator's tmr_role label is set automatically by _create_tmr_agent
    # (from AgentKind.INTEGRATOR), distinguishing it from testing agents in
    # `mngr ls` and during reintegrate.
    integrator_agent_type = opts.integrator_type if opts.integrator_type is not None else opts.agent_type
    integrator_templates = opts.integrator_template if opts.integrator_template else opts.agent_template
    integrator_config = TmrLaunchConfig(
        source_dir=source_dir,
        source_host=source_host,
        base_commit=base_commit,
        agent_type=AgentTypeName(integrator_agent_type),
        provider_name=ProviderInstanceName(opts.integrator_provider),
        env_options=env_options,
        label_options=label_options,
        templates=integrator_templates,
        additional_authorized_keys=opts.additional_authorized_keys,
    )
    integrator_meta = _run_integrator_phase(
        test_agent_metadata, integrator_config, mngr_ctx, opts, output_dir, run, base_commit=base_commit
    )
    integrated_branch = integrator_meta.branch_name if integrator_meta is not None else None
    report_path = generate_html_report(
        test_agent_metadata,
        output_dir,
        integrator_metadata=integrator_meta,
        run_commands=_build_run_commands(run, integrated_branch),
    )
    _emit_report_path(output_dir / "index.html", output_opts)
    _emit_report_url(maybe_upload_report(report_path, run), output_opts)
    _emit_integrator_branch(integrated_branch, output_opts)

    _print_run_commands(run, output_opts, integrated_branch)

    # If no test agent ever launched successfully, surface a non-zero exit so
    # callers (especially CI) don't read the run as successful. The HTML
    # report (already written above) still contains the per-agent error
    # summaries needed to diagnose the launch failures.
    if test_node_ids and not agent_infos:
        raise MngrError(
            f"All {len(launch_failures)} test agent launches failed; "
            "see the HTML report for per-agent error summaries."
        )


def _build_run_commands(run_name: str, integrated_branch: str | None = None) -> list[tuple[str, str]]:
    """Build a list of (label, command) pairs for the run."""
    commands = [
        ("List agents from this run", f"mngr ls --include 'labels.tmr_run_name == \"{run_name}\"'"),
        ("Reintegrate", f"mngr tmr --reintegrate --run-name {run_name}"),
    ]
    if integrated_branch is not None:
        commands.append(("Push integrated branch", f"git push origin {integrated_branch}"))
    return commands


def _print_run_commands(run_name: str, output_opts: OutputOptions, integrated_branch: str | None = None) -> None:
    """Print useful commands for managing a TMR run's agents.

    Only emits in HUMAN output mode; in JSON/JSONL the run name and integrator
    branch are already exposed via structured events, and unguarded
    `write_human_line` calls would pollute the structured stream.
    """
    if output_opts.output_format != OutputFormat.HUMAN:
        return
    write_human_line("")
    for label, cmd in _build_run_commands(run_name, integrated_branch):
        write_human_line("{}:", label)
        write_human_line("  {}", cmd)


CommandHelpMetadata(
    key="tmr",
    one_line_description="Run and fix tests in parallel using agents (test map-reduce)",
    synopsis="mngr tmr [TEST_PATHS...] [-- TESTING_FLAGS...] [--provider <PROVIDER>] [--use-snapshot] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]",
    description="""This command implements a map-reduce pattern for tests:

1. Collects tests using pytest --collect-only, passing through all arguments.
2. Launches one agent per test. Each agent runs the test and, if it fails,
   attempts to diagnose and fix either the test code or the implementation.
3. Polls agents until all finish or individually time out (per-agent timeout).
   An HTML report is updated continuously during polling.
4. For successful fixes, pulls the agent's code changes into branches
   named mngr-tmr/*.
5. If any fixes succeeded, launches an integrator agent to merge all fix
   branches into a single integrated branch (mngr-tmr/integrated-*).
6. Generates a final HTML report summarizing all outcomes with markdown
   summaries, including the integrated branch name if applicable.

Arguments before -- are test paths/patterns (positional). Arguments after -- are
pytest testing flags shared between discovery and individual test runs. For example:

  mngr tmr tests/e2e -- -m release

This discovers tests with `pytest --collect-only tests/e2e -m release` and runs
each test with `pytest tests/e2e/test_foo.py::test_bar -m release`.

Use --provider to run agents on a specific provider (e.g. docker, modal).
Use --use-snapshot with remote providers to build and provision one host first,
snapshot it, then launch all remaining agents from the snapshot (much faster).
Use --env to pass environment variables and --label to tag all agents.
Use --prompt-suffix to append custom instructions to the agent prompt.
Use --max-parallel-agents to limit how many agents run simultaneously (0 = no limit).

Each agent writes its result to .test_output/testing_agent_outcome.json (in its work directory)
with a structured JSON containing: changes (list of kind/status/summary), errored flag,
tests_passing_before/after booleans, and a markdown summary.""",
    examples=(
        ("Run all tests in current directory", "mngr tmr"),
        ("Run tests in a specific file", "mngr tmr tests/test_foo.py"),
        ("Run tests with a marker", "mngr tmr tests/e2e -- -m release"),
        ("Use Docker provider", "mngr tmr --provider docker tests/"),
        ("Modal with snapshot", "mngr tmr --provider modal --use-snapshot tests/"),
        ("Pass env vars and labels", "mngr tmr --env API_KEY=xxx --label batch=run1"),
        ("Limit to 4 concurrent agents", "mngr tmr --max-parallel-agents 4 tests/"),
        ("Custom poll interval", "mngr tmr --poll-interval 30"),
        ("Specify output location", "mngr tmr --output-dir reports/run-1"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List agents"),
        ("pull", "Pull files or git commits from an agent"),
    ),
).register()

add_pager_help_option(tmr)
