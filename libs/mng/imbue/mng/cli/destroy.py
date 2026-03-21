import threading
from collections.abc import Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.api.data_types import GcResourceTypes
from imbue.mng.api.discovery_events import emit_agent_destroyed
from imbue.mng.api.discovery_events import emit_discovery_events_for_host
from imbue.mng.api.discovery_events import emit_host_destroyed
from imbue.mng.api.find import AgentMatch
from imbue.mng.api.gc import gc as api_gc
from imbue.mng.api.providers import get_all_provider_instances
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.agent_addr import find_agents_by_addresses
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_format_template_lines
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import HostConnectionError
from imbue.mng.errors import HostOfflineError
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId
from imbue.mng.primitives import OutputFormat
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.utils.git_utils import find_source_repo_of_worktree
from imbue.mng.utils.git_utils import remove_worktree


class _OfflineHostToDestroy(FrozenModel):
    """An offline host where all agents are targeted for destruction."""

    model_config = {**FrozenModel.model_config, "arbitrary_types_allowed": True}

    host: HostInterface = Field(description="The offline host to destroy")
    provider: ProviderInstanceInterface = Field(description="The provider instance for this host")
    agent_names: list[AgentName] = Field(description="Names of agents on this host targeted for destruction")
    agent_ids: list[AgentId] = Field(description="IDs of agents on this host targeted for destruction")


class _DestroyTargets(FrozenModel):
    """Result of finding agents/hosts to destroy."""

    model_config = {**FrozenModel.model_config, "arbitrary_types_allowed": True}

    online_agents: list[tuple[AgentInterface, OnlineHostInterface]] = Field(
        description="Agents on online hosts to destroy, paired with their host"
    )
    offline_hosts: list[_OfflineHostToDestroy] = Field(
        description="Offline hosts where all agents are targeted for destruction"
    )


def get_agent_name_from_session(session_name: str, prefix: str) -> str | None:
    """Extract the agent name from a tmux session name.

    The session name is expected to be in the format "{prefix}{agent_name}".
    Returns the agent name if the session matches the prefix, or None if the
    session name doesn't match the expected prefix format.
    """
    if not session_name:
        logger.debug("Failed to extract agent name: empty session name provided")
        return None

    # Check if the session name starts with our prefix
    if not session_name.startswith(prefix):
        logger.debug(
            "Failed to extract agent name: session name '{}' doesn't start with mng prefix '{}'",
            session_name,
            prefix,
        )
        return None

    # Extract the agent name by removing the prefix
    agent_name = session_name[len(prefix) :]
    if not agent_name:
        logger.debug(
            "Failed to extract agent name: session name '{}' has empty agent name after stripping prefix", session_name
        )
        return None

    logger.debug("Extracted agent name '{}' from session '{}'", agent_name, session_name)
    return agent_name


class DestroyCliOptions(CommonCliOptions):
    """Options passed from the CLI to the destroy command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the destroy() function itself.
    """

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    force: bool
    destroy_all: bool
    dry_run: bool
    gc: bool
    remove_created_branch: bool
    allow_worktree_removal: bool
    sessions: tuple[str, ...]
    # Planned features (not yet implemented)
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    stdin: bool


@click.command(name="destroy")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to destroy (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "destroy_all",
    is_flag=True,
    help="Destroy all agents",
)
@optgroup.option(
    "--session",
    "sessions",
    multiple=True,
    help="Tmux session name to destroy (can be specified multiple times). The agent name is extracted by "
    "stripping the configured prefix from the session name.",
)
@optgroup.option(
    "--include",
    multiple=True,
    help="Filter agents to destroy by CEL expression (repeatable). [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude agents matching CEL expression from destruction (repeatable). [future]",
)
@optgroup.option(
    "--stdin",
    is_flag=True,
    help="Read agent names/IDs from stdin, one per line. [future]",
)
@optgroup.group("Behavior")
@optgroup.option(
    "-f",
    "--force",
    is_flag=True,
    help="Skip confirmation prompts and force destroy running agents",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be destroyed without actually destroying",
)
@optgroup.option(
    "--gc/--no-gc",
    default=True,
    help="Run garbage collection after destroying agents to clean up orphaned resources (default: enabled)",
)
@optgroup.option(
    "-b",
    "--remove-created-branch",
    is_flag=True,
    help="Delete the git branch that mng created for the agent's work directory",
)
@optgroup.option(
    "--allow-worktree-removal/--no-allow-worktree-removal",
    default=True,
    help="Allow removal of the git worktree directory (default: enabled)",
)
@add_common_options
@click.pass_context
def destroy(ctx: click.Context, **kwargs) -> None:
    # Setup command context (config, logging, output options)
    # This loads the config, applies defaults, and creates the final options
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="destroy",
        command_class=DestroyCliOptions,
        is_format_template_supported=True,
    )

    # Filter agents to destroy using CEL expressions like:
    # --include 'name.startsWith("test-")' or --include 'host.provider == "docker"'
    # See mng list --include for the pattern to follow
    if opts.include:
        raise NotImplementedError(
            "The --include option is not yet implemented. See https://github.com/imbue-ai/mng/issues/XXX for progress."
        )
    # Exclude agents matching CEL expressions from destruction:
    # --exclude 'state == "RUNNING"' to skip running agents
    # See mng list --exclude for the pattern to follow
    if opts.exclude:
        raise NotImplementedError(
            "The --exclude option is not yet implemented. See https://github.com/imbue-ai/mng/issues/XXX for progress."
        )
    # Read agent names/IDs from stdin to allow piping agent lists:
    # mng list --format jsonl | jq -r .name | mng destroy --stdin
    if opts.stdin:
        raise NotImplementedError(
            "The --stdin option is not yet implemented. See https://github.com/imbue-ai/mng/issues/XXX for progress."
        )

    # Validate input
    agent_identifiers = list(opts.agents) + list(opts.agent_list)

    # Handle --session option by extracting agent names from session names
    if opts.sessions:
        if agent_identifiers or opts.destroy_all:
            raise UserInputError("Cannot specify --session with agent names or --all")
        for session_name in opts.sessions:
            agent_name = get_agent_name_from_session(session_name, mng_ctx.config.prefix)
            if agent_name is None:
                raise UserInputError(
                    f"Session '{session_name}' does not match the expected format. "
                    f"Session names should start with the configured prefix '{mng_ctx.config.prefix}'."
                )
            agent_identifiers.append(agent_name)

    if not agent_identifiers and not opts.destroy_all:
        raise UserInputError("Must specify at least one agent or use --all")

    if agent_identifiers and opts.destroy_all:
        raise UserInputError("Cannot specify both agent names and --all")

    # Find agents to destroy
    try:
        targets = _find_agents_to_destroy(
            agent_identifiers=agent_identifiers,
            destroy_all=opts.destroy_all,
            mng_ctx=mng_ctx,
        )
    except AgentNotFoundError as e:
        if opts.force:
            targets = _DestroyTargets(online_agents=[], offline_hosts=[])
            _output(f"Error destroying agent(s): {e}", output_opts)
        else:
            raise

    if not targets.online_agents and not targets.offline_hosts:
        _output("No agents found to destroy", output_opts)
        return

    # Handle dry-run mode
    if opts.dry_run:
        _output_targets(targets, "Would destroy:", output_opts)
        return

    # Confirm destruction if not forced
    if not opts.force:
        _confirm_destruction(targets)

    # Destroy all targets (online agents + offline hosts) in parallel
    destroyed_agents: list[AgentName] = []
    worktrees_to_remove: list[tuple[Path, Path]] = []
    branches_to_remove: list[tuple[str, Path]] = []
    results_lock = threading.Lock()

    with ConcurrencyGroupExecutor(
        parent_cg=mng_ctx.concurrency_group, name="destroy_agents", max_workers=32
    ) as executor:
        futures: list[Future[None]] = []
        for agent, host in targets.online_agents:
            futures.append(
                executor.submit(
                    _destroy_single_online_agent,
                    agent,
                    host,
                    opts,
                    output_opts,
                    mng_ctx,
                    results_lock,
                    destroyed_agents,
                    worktrees_to_remove,
                    branches_to_remove,
                )
            )
        for offline in targets.offline_hosts:
            futures.append(
                executor.submit(
                    _destroy_single_offline_host,
                    offline,
                    output_opts,
                    mng_ctx,
                    results_lock,
                    destroyed_agents,
                )
            )

    # Re-raise any unexpected exceptions from destroy threads
    for future in futures:
        future.result()

    # Remove worktrees (must happen before branch deletion)
    for work_dir, source_repo_path in worktrees_to_remove:
        try:
            remove_worktree(work_dir, source_repo_path, mng_ctx.concurrency_group)
            _output(f"Removed worktree: {work_dir}", output_opts)
        except ProcessError as e:
            logger.warning("Failed to remove worktree {}: {}", work_dir, e)

    # Delete created branches (after worktree removal)
    for created_branch, source_repo_path in branches_to_remove:
        _remove_created_branch(created_branch, source_repo_path, mng_ctx.concurrency_group, output_opts)

    # Run garbage collection if enabled
    if opts.gc and not opts.dry_run and destroyed_agents:
        _run_post_destroy_gc(mng_ctx=mng_ctx, output_opts=output_opts)

    # Output final result
    _output_result(destroyed_agents, output_opts)


def _find_agents_to_destroy(
    agent_identifiers: Sequence[str],
    destroy_all: bool,
    mng_ctx: MngContext,
) -> _DestroyTargets:
    """Find all agents to destroy.

    Uses find_agents_by_addresses for matching (supports NAME@HOST.PROVIDER syntax),
    then partitions results into online agents vs offline hosts.

    Returns _DestroyTargets containing online agents and offline hosts to destroy.
    Raises AgentNotFoundError if any specified identifier does not match an agent.
    """
    # Step 1: Find matching agents using the shared address-aware resolution.
    # This handles address parsing, name/ID matching, and host/provider filtering.
    # include_destroyed=True so we can find and clean up agents on already-destroyed hosts.
    matches = find_agents_by_addresses(
        raw_identifiers=agent_identifiers,
        filter_all=destroy_all,
        target_state=None,
        mng_ctx=mng_ctx,
        include_destroyed=True,
    )

    # Step 2: Partition matches into online agents vs offline hosts.
    return _partition_destroy_targets(matches, mng_ctx)


def _partition_destroy_targets(
    matches: Sequence[AgentMatch],
    mng_ctx: MngContext,
) -> _DestroyTargets:
    """Partition matched agents into online agents and offline hosts to destroy.

    For online hosts, resolves each matched agent to its AgentInterface.
    For offline hosts, verifies ALL agents on the host are being destroyed
    (since individual agent destruction requires the host to be online).

    Each host is resolved in parallel via a ConcurrencyGroupExecutor.
    """
    online_agents: list[tuple[AgentInterface, OnlineHostInterface]] = []
    offline_hosts: list[_OfflineHostToDestroy] = []
    results_lock = threading.Lock()

    # Group matched agent IDs by host for the offline "all targeted" check
    matched_ids_by_host: dict[str, set[AgentId]] = {}
    for match in matches:
        matched_ids_by_host.setdefault(str(match.host_id), set()).add(match.agent_id)

    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mng_ctx.concurrency_group, name="partition_destroy_targets", max_workers=32
    ) as executor:
        for host_id_str, matched_ids in matched_ids_by_host.items():
            futures.append(
                executor.submit(
                    _resolve_host_for_partition,
                    host_id_str,
                    matched_ids,
                    matches,
                    mng_ctx,
                    results_lock,
                    online_agents,
                    offline_hosts,
                )
            )

    # Re-raise any exceptions (e.g. HostOfflineError from partial targeting)
    for future in futures:
        future.result()

    return _DestroyTargets(online_agents=online_agents, offline_hosts=offline_hosts)


def _resolve_host_for_partition(
    host_id_str: str,
    matched_ids: set[AgentId],
    matches: Sequence[AgentMatch],
    mng_ctx: MngContext,
    results_lock: threading.Lock,
    online_agents: list[tuple[AgentInterface, OnlineHostInterface]],
    offline_hosts: list[_OfflineHostToDestroy],
) -> None:
    """Resolve a single host and categorize its agents for destruction."""
    # Get the provider from any match on this host
    provider_name = next(m.provider_name for m in matches if str(m.host_id) == host_id_str)
    provider = get_provider_instance(provider_name, mng_ctx)
    host_interface = provider.get_host(HostId(host_id_str))

    match host_interface:
        case OnlineHostInterface() as online_host:
            try:
                agents = online_host.get_agents()
            except HostConnectionError as e:
                logger.warning(
                    "Failed to connect to host {} to verify agent status. Treating host as offline: {}",
                    host_id_str,
                    str(e),
                )
                offline_host_interface = host_interface.to_offline_host()
                with results_lock:
                    _check_all_agents_targeted_on_offline_host(
                        offline_host_interface, matched_ids, host_id_str, offline_hosts, provider
                    )
                return

            with results_lock:
                for agent in agents:
                    if agent.id in matched_ids:
                        online_agents.append((agent, online_host))
        case HostInterface() as offline_host:
            with results_lock:
                _check_all_agents_targeted_on_offline_host(
                    offline_host, matched_ids, host_id_str, offline_hosts, provider
                )
        case _ as unreachable:
            assert_never(unreachable)


def _destroy_single_online_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    opts: DestroyCliOptions,
    output_opts: OutputOptions,
    mng_ctx: MngContext,
    results_lock: threading.Lock,
    destroyed_agents: list[AgentName],
    worktrees_to_remove: list[tuple[Path, Path]],
    branches_to_remove: list[tuple[str, Path]],
) -> None:
    """Destroy a single agent on an online host. Thread-safe."""
    try:
        if agent.is_running() and not opts.force:
            _output(
                f"Agent {agent.name} is running. Use --force to destroy running agents.",
                output_opts,
            )
            return

        # Read worktree info before destroy removes the work_dir
        source_repo_path = find_source_repo_of_worktree(agent.work_dir)
        if source_repo_path is not None:
            if opts.allow_worktree_removal:
                with results_lock:
                    worktrees_to_remove.append((agent.work_dir, source_repo_path))
            if opts.remove_created_branch:
                created_branch = agent.get_created_branch_name()
                if created_branch is not None:
                    with results_lock:
                        branches_to_remove.append((created_branch, source_repo_path))

        mng_ctx.pm.hook.on_before_agent_destroy(agent=agent, host=host)
        host.destroy_agent(agent)
        mng_ctx.pm.hook.on_agent_destroyed(agent=agent, host=host)
        with results_lock:
            destroyed_agents.append(agent.name)
        _output(f"Destroyed agent: {agent.name}", output_opts)

        # Emit agent_destroyed event, then re-emit remaining host state
        emit_agent_destroyed(mng_ctx.config, agent.id, host.id)
        emit_discovery_events_for_host(mng_ctx.config, host)

    except MngError as e:
        _output(f"Error destroying agent {agent.name}: {e}", output_opts)


def _destroy_single_offline_host(
    offline: _OfflineHostToDestroy,
    output_opts: OutputOptions,
    mng_ctx: MngContext,
    results_lock: threading.Lock,
    destroyed_agents: list[AgentName],
) -> None:
    """Destroy a single offline host and all its agents. Thread-safe."""
    try:
        _output(f"Destroying offline host with {len(offline.agent_names)} agent(s)...", output_opts)
        mng_ctx.pm.hook.on_before_host_destroy(host=offline.host)
        offline.provider.destroy_host(offline.host)
        mng_ctx.pm.hook.on_host_destroyed(host=offline.host)
        with results_lock:
            destroyed_agents.extend(offline.agent_names)
        for name in offline.agent_names:
            _output(f"Destroyed agent: {name} (via host destruction)", output_opts)

        # Emit host_destroyed event with all agent IDs
        emit_host_destroyed(mng_ctx.config, offline.host.id, offline.agent_ids)
    except MngError as e:
        _output(f"Error destroying offline host: {e}", output_opts)


def _check_all_agents_targeted_on_offline_host(
    offline_host: HostInterface,
    matched_ids: set[AgentId],
    host_id_str: str,
    offline_hosts: list[_OfflineHostToDestroy],
    provider: BaseProviderInstance,
) -> None:
    """Verify all agents on an offline host are targeted, then queue it for destruction.

    Offline hosts can only be destroyed as a whole -- individual agent destruction
    requires the host to be online. Raises HostOfflineError if only some agents
    are targeted.
    """
    all_agent_refs = offline_host.discover_agents()
    all_targeted = all(ref.agent_id in matched_ids for ref in all_agent_refs)
    if all_targeted:
        offline_hosts.append(
            _OfflineHostToDestroy(
                host=offline_host,
                provider=provider,
                agent_names=[ref.agent_name for ref in all_agent_refs],
                agent_ids=[ref.agent_id for ref in all_agent_refs],
            )
        )
    else:
        raise HostOfflineError(
            f"Host '{host_id_str}' is offline. Cannot destroy individual agents on an "
            f"offline host. Either start the host first, or destroy all "
            f"{len(all_agent_refs)} agent(s) on this host."
        )


def _confirm_destruction(targets: _DestroyTargets) -> None:
    """Prompt user to confirm destruction of agents."""
    write_human_line("\nThe following agents will be destroyed:")
    for agent, _ in targets.online_agents:
        write_human_line("  - {}", agent.name)
    for offline in targets.offline_hosts:
        for name in offline.agent_names:
            write_human_line("  - {} (on offline host)", name)

    write_human_line("\nThis action is irreversible!")

    if not click.confirm("Are you sure you want to continue?"):
        raise click.Abort()


def _output_targets(
    targets: _DestroyTargets,
    prefix: str,
    output_opts: OutputOptions,
) -> None:
    """Output a list of agents to destroy."""
    agent_data = [
        {"agent_id": str(agent.id), "agent_name": str(agent.name), "host_id": str(host.id)}
        for agent, host in targets.online_agents
    ]
    for offline in targets.offline_hosts:
        for name in offline.agent_names:
            agent_data.append({"agent_name": str(name), "host_id": str(offline.host.id), "host_offline": True})

    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"agents": agent_data})
        case OutputFormat.JSONL:
            emit_event("agents_list", {"agents": agent_data}, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("\n{}", prefix)
            for agent, host in targets.online_agents:
                write_human_line("  - {} (on host {})", agent.name, host.id)
            for offline in targets.offline_hosts:
                for name in offline.agent_names:
                    write_human_line("  - {} (on offline host {})", name, offline.host.id)
        case _ as unreachable:
            assert_never(unreachable)


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message according to the format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(destroyed_agents: Sequence[AgentName], output_opts: OutputOptions) -> None:
    """Output the final result."""
    if output_opts.format_template is not None:
        items = [{"name": str(n)} for n in destroyed_agents]
        emit_format_template_lines(output_opts.format_template, items)
        return
    result_data = {"destroyed_agents": [str(n) for n in destroyed_agents], "count": len(destroyed_agents)}
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("destroy_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if destroyed_agents:
                write_human_line("\nSuccessfully destroyed {} agent(s)", len(destroyed_agents))
        case _ as unreachable:
            assert_never(unreachable)


def _remove_created_branch(
    branch_name: str,
    source_repo_path: Path,
    cg: ConcurrencyGroup,
    output_opts: OutputOptions,
) -> None:
    """Delete a git branch from the source repository.

    Called after worktree removal, so git should allow the branch deletion.
    Failures are logged as warnings but do not fail the destroy operation.
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "-C", str(source_repo_path), "branch", "-D", branch_name],
            is_checked_after=False,
        )
        if result.returncode == 0:
            _output(f"Deleted branch: {branch_name}", output_opts)
        else:
            logger.warning("Failed to delete branch {}: {}", branch_name, result.stderr.strip())
    except ProcessError as e:
        logger.warning("Failed to delete branch {}: {}", branch_name, e)


def _run_post_destroy_gc(mng_ctx: MngContext, output_opts: OutputOptions) -> None:
    """Run garbage collection after destroying agents.

    This cleans up orphaned host-level resources (machines, work dirs, snapshots, volumes).
    Errors are logged but don't prevent destroy from reporting success.
    """
    try:
        _output("Garbage collecting...", output_opts)

        providers = get_all_provider_instances(mng_ctx)

        resource_types = GcResourceTypes(
            is_machines=True,
            is_work_dirs=True,
            is_snapshots=True,
            is_volumes=True,
            is_logs=False,
            is_build_cache=False,
        )

        result = api_gc(
            mng_ctx=mng_ctx,
            providers=providers,
            resource_types=resource_types,
            dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        _output("Garbage collecting... done.", output_opts)

        if result.errors:
            logger.warning("Garbage collection completed with {} error(s)", len(result.errors))
            for error in result.errors:
                logger.warning("  - {}", error)

    except MngError as e:
        logger.warning("Garbage collection failed: {}", e)
        logger.warning("This does not affect the destroy operation, which completed successfully")


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="destroy",
    one_line_description="Destroy agent(s) and clean up resources",
    synopsis="mng [destroy|rm] [AGENTS...] [--agent <AGENT>] [--all] [--session <SESSION>] [-f|--force] [--dry-run] [-b|--remove-created-branch]",
    description="""When the last agent on a host is destroyed, the host itself is also destroyed
(including containers, volumes, snapshots, and any remote infrastructure).

Use with caution! This operation is irreversible.

By default, running agents cannot be destroyed. Use --force to stop and destroy
running agents. The command will prompt for confirmation before destroying
agents unless --force is specified.

Supports custom format templates via --format. Available fields: name.""",
    aliases=("rm",),
    examples=(
        ("Destroy an agent by name", "mng destroy my-agent"),
        ("Destroy multiple agents", "mng destroy agent1 agent2 agent3"),
        ("Destroy all agents", "mng destroy --all --force"),
        ("Preview what would be destroyed", "mng destroy my-agent --dry-run"),
        ("Destroy using --agent flag (repeatable)", "mng destroy --agent my-agent --agent another-agent"),
        ("Destroy by tmux session name", "mng destroy --session mng-my-agent"),
        ("Custom format template output", "mng destroy --all --force --format '{name}'"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List existing agents"),
        ("gc", "Garbage collect orphaned resources"),
    ),
    additional_sections=(
        (
            "Related Documentation",
            """- [Resource Cleanup Options](../generic/resource_cleanup.md) - Control which associated resources are destroyed
- [Multi-target Options](../generic/multi_target.md) - Behavior when targeting multiple agents""",
        ),
    ),
).register()

# Add pager-enabled help option to the destroy command
add_pager_help_option(destroy)
