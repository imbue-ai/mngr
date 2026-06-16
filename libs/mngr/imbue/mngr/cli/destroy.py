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
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.data_types import GcResourceTypes
from imbue.mngr.api.discovery_events import emit_agent_destroyed
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.discovery_events import emit_host_destroyed
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.gc import gc as api_gc
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import parse_agent_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.exit_codes import exit_code_for_failures
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.git_utils import delete_git_branch
from imbue.mngr.utils.git_utils import find_source_repo_of_worktree
from imbue.mngr.utils.thread_cleanup import mngr_executor


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
    online_hosts_with_provider: list[tuple[OnlineHostInterface, ProviderInstanceInterface]] = Field(
        default_factory=list,
        description=(
            "Deduplicated online hosts that had at least one agent targeted for "
            "destruction, paired with their provider. Used after the destroy loop "
            "to force-destroy hosts whose last agent was just destroyed (the "
            "documented `mngr destroy` contract). One entry per unique host id."
        ),
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
            "Failed to extract agent name: session name '{}' doesn't start with mngr prefix '{}'",
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
    agent_list: tuple[AgentAddress, ...]
    force: bool
    gc: bool
    remove_created_branch: bool
    allow_worktree_removal: bool
    sessions: tuple[str, ...]
    dry_run: bool


@click.command(name="destroy")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    type=AGENT_ADDRESS,
    multiple=True,
    help="Agent address (NAME[@HOST[.PROVIDER]]) to destroy (can be specified multiple times)",
)
@optgroup.option(
    "--session",
    "sessions",
    multiple=True,
    help="Tmux session name to destroy (can be specified multiple times). The agent name is extracted by "
    "stripping the configured prefix from the session name.",
)
@optgroup.group("Behavior")
@optgroup.option(
    "-f",
    "--force",
    is_flag=True,
    help="Skip confirmation prompts and force destroy running agents",
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
    help="Delete the git branch that mngr created for the agent's work directory",
)
@optgroup.option(
    "--allow-worktree-removal/--no-allow-worktree-removal",
    default=True,
    help="Allow GC to remove the git worktree directory (default: enabled)",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be destroyed without actually destroying anything",
)
@add_common_options
@click.pass_context
def destroy(ctx: click.Context, **kwargs) -> None:
    # Setup command context (config, logging, output options)
    # This loads the config, applies defaults, and creates the final options
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="destroy",
        command_class=DestroyCliOptions,
        is_format_template_supported=True,
    )

    # Validate input. Variadic positional is parsed here (after stdin expansion);
    # --agent is already typed by Click.
    expanded_positional = parse_agent_addresses_or_raise(expand_stdin_placeholder(opts.agents))
    agent_addresses: list[AgentAddress] = expanded_positional + list(opts.agent_list)

    # Handle --session option by extracting agent names from session names
    if opts.sessions:
        if agent_addresses:
            raise UserInputError("Cannot specify --session with agent names")
        for session_name in opts.sessions:
            agent_name = get_agent_name_from_session(session_name, mngr_ctx.config.prefix)
            if agent_name is None:
                raise UserInputError(
                    f"Session '{session_name}' does not match the expected format. "
                    f"Session names should start with the configured prefix '{mngr_ctx.config.prefix}'."
                )
            agent_addresses.append(parse_agent_addresses_or_raise([agent_name])[0])

    if not agent_addresses:
        if STDIN_PLACEHOLDER not in opts.agents:
            raise UserInputError("Must specify at least one agent (use '-' to read from stdin)")
        return

    # Find agents to destroy
    try:
        targets = _find_agents_to_destroy(
            addresses=agent_addresses,
            mngr_ctx=mngr_ctx,
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

    # Dry-run: report what would be destroyed without touching anything.
    if opts.dry_run:
        _emit_dry_run_output(targets, output_opts)
        return

    # Confirm destruction if not forced
    if not opts.force:
        _confirm_destruction(targets)

    # Destroy all targets (online agents + offline hosts) in parallel
    destroyed_agents: list[AgentName] = []
    branches_to_remove: list[tuple[str, Path]] = []
    # Shared accumulator of real cleanup failures (resources left behind). Mutated
    # under ``results_lock`` exactly like ``destroyed_agents``.
    failures: list[CleanupFailure] = []
    results_lock = threading.Lock()

    with mngr_executor(parent_cg=mngr_ctx.concurrency_group, name="destroy_agents", max_workers=32) as executor:
        futures: list[Future[None]] = []
        for agent, host in targets.online_agents:
            futures.append(
                executor.submit(
                    _destroy_single_online_agent,
                    agent,
                    host,
                    opts,
                    output_opts,
                    mngr_ctx,
                    results_lock,
                    destroyed_agents,
                    branches_to_remove,
                    failures,
                )
            )
        for offline in targets.offline_hosts:
            futures.append(
                executor.submit(
                    _destroy_single_offline_host,
                    offline,
                    output_opts,
                    mngr_ctx,
                    results_lock,
                    destroyed_agents,
                    failures,
                )
            )

    # Re-raise any unexpected exceptions from destroy threads
    for future in futures:
        future.result()

    # Force-destroy hosts whose last agent was just destroyed. The destroy
    # command's documented contract is "when the last agent on a host is
    # destroyed, the host itself is also destroyed". The post-destroy GC
    # pass below technically delivers this, but only for hosts past
    # ``min_online_host_age_seconds`` (default 10 minutes) -- so a host
    # destroyed within minutes of creation would otherwise leak its
    # cloud-side resources (e.g. a still-active imbue_cloud lease) until
    # the destroyed-host grace period (default 7 days) eventually
    # triggers ``provider.delete_host``. Forcing the destroy here closes
    # that gap and makes the user-visible behaviour match the docs for
    # all provider types.
    _destroy_emptied_hosts(
        online_hosts_with_provider=targets.online_hosts_with_provider,
        mngr_ctx=mngr_ctx,
        output_opts=output_opts,
        results_lock=results_lock,
        failures=failures,
    )

    # Run garbage collection if enabled.  Worktree cleanup is GC's job:
    # `gc._get_orphaned_work_dirs` already enforces the only-mngr-generated /
    # not-still-referenced safety predicate, so the destroy command does not
    # touch worktrees inline.  Branch deletion runs after GC so that any
    # worktree GC removes is no longer holding the branch checked out.
    if opts.gc and destroyed_agents:
        _run_post_destroy_gc(
            mngr_ctx=mngr_ctx,
            output_opts=output_opts,
            include_work_dirs=opts.allow_worktree_removal,
        )

    # Delete created branches (after GC, so the worktree that was holding
    # the branch checked out is gone and `git branch -D` can succeed).
    for created_branch, source_repo_path in branches_to_remove:
        _remove_created_branch(created_branch, source_repo_path, mngr_ctx.concurrency_group, output_opts)

    # Output final result, then exit with a cause-specific code if any real
    # cleanup failures (resources left behind) remain.
    _output_result(destroyed_agents, failures, output_opts)
    ctx.exit(exit_code_for_failures(failures))


def _find_agents_to_destroy(
    addresses: Sequence[AgentAddress],
    mngr_ctx: MngrContext,
) -> _DestroyTargets:
    """Find all agents to destroy.

    Returns _DestroyTargets containing online agents and offline hosts to destroy.
    Raises AgentNotFoundError if any specified address does not match an agent.
    """
    # include_destroyed=True so we can find and clean up agents on already-destroyed hosts.
    matches = find_all_agents(
        addresses=addresses,
        filter_all=False,
        target_state=None,
        mngr_ctx=mngr_ctx,
        include_destroyed=True,
    )

    # Partition matches into online agents vs offline hosts.
    return _partition_destroy_targets(matches, mngr_ctx)


def _partition_destroy_targets(
    matches: Sequence[AgentMatch],
    mngr_ctx: MngrContext,
) -> _DestroyTargets:
    """Partition matched agents into online agents and offline hosts to destroy.

    For online hosts, resolves each matched agent to its AgentInterface.
    For offline hosts, verifies ALL agents on the host are being destroyed
    (since individual agent destruction requires the host to be online).

    Each host is resolved in parallel via a ConcurrencyGroupExecutor.
    """
    online_agents: list[tuple[AgentInterface, OnlineHostInterface]] = []
    offline_hosts: list[_OfflineHostToDestroy] = []
    online_hosts_with_provider: list[tuple[OnlineHostInterface, ProviderInstanceInterface]] = []
    results_lock = threading.Lock()

    # Group matched agent IDs by host for the offline "all targeted" check
    matched_ids_by_host: dict[str, set[AgentId]] = {}
    for match in matches:
        matched_ids_by_host.setdefault(str(match.host_id), set()).add(match.agent_id)

    futures: list[Future[None]] = []
    with mngr_executor(
        parent_cg=mngr_ctx.concurrency_group, name="partition_destroy_targets", max_workers=32
    ) as executor:
        for host_id_str, matched_ids in matched_ids_by_host.items():
            futures.append(
                executor.submit(
                    _resolve_host_for_partition,
                    host_id_str,
                    matched_ids,
                    matches,
                    mngr_ctx,
                    results_lock,
                    online_agents,
                    offline_hosts,
                    online_hosts_with_provider,
                )
            )

    # Re-raise any exceptions (e.g. HostOfflineError from partial targeting)
    for future in futures:
        future.result()

    return _DestroyTargets(
        online_agents=online_agents,
        offline_hosts=offline_hosts,
        online_hosts_with_provider=online_hosts_with_provider,
    )


def _resolve_host_for_partition(
    host_id_str: str,
    matched_ids: set[AgentId],
    matches: Sequence[AgentMatch],
    mngr_ctx: MngrContext,
    results_lock: threading.Lock,
    online_agents: list[tuple[AgentInterface, OnlineHostInterface]],
    offline_hosts: list[_OfflineHostToDestroy],
    online_hosts_with_provider: list[tuple[OnlineHostInterface, ProviderInstanceInterface]],
) -> None:
    """Resolve a single host and categorize its agents for destruction."""
    # Get the provider from any match on this host
    provider_name = next(m.provider_name for m in matches if str(m.host_id) == host_id_str)
    provider = get_provider_instance(provider_name, mngr_ctx)
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
                _check_all_agents_targeted_on_offline_host(
                    offline_host_interface, matched_ids, host_id_str, offline_hosts, provider, results_lock
                )
                return

            # Reconcile discover-vs-on-host disagreement: when every matched
            # agent is a "ghost" (returned by discover but absent from the
            # host's own ``get_agents()`` listing), the agents have already
            # been destroyed on-host but the provider's discovery view (e.g.
            # the imbue_cloud connector's lease list) hasn't caught up.
            # Escalate to host-level destruction so ``provider.destroy_host``
            # runs and reconciles the cloud-side state (releases the lease,
            # destroys the VPS, etc.). Only escalate when ALL matched ids
            # are ghosts -- a mix of live + ghost ids on a multi-agent host
            # still goes through per-agent destroy for the live ones (the
            # ghosts get the existing silent-drop behaviour; the per-host
            # auto-destroy in the main destroy loop covers the empty case
            # if the live destroys leave the host empty).
            live_agent_ids = {a.id for a in agents}
            if matched_ids and matched_ids.isdisjoint(live_agent_ids):
                offline_host_interface = online_host.to_offline_host()
                _check_all_agents_targeted_on_offline_host(
                    offline_host_interface, matched_ids, host_id_str, offline_hosts, provider, results_lock
                )
                return

            with results_lock:
                added_any = False
                for agent in agents:
                    if agent.id in matched_ids:
                        online_agents.append((agent, online_host))
                        added_any = True
                # Track this host so the destroy loop's post-pass can check
                # whether the host became empty (last-agent-destroyed
                # auto-destroy contract).
                if added_any:
                    online_hosts_with_provider.append((online_host, provider))
        case HostInterface() as offline_host:
            _check_all_agents_targeted_on_offline_host(
                offline_host, matched_ids, host_id_str, offline_hosts, provider, results_lock
            )
        case _ as unreachable:
            assert_never(unreachable)


def _destroy_single_online_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    opts: DestroyCliOptions,
    output_opts: OutputOptions,
    mngr_ctx: MngrContext,
    results_lock: threading.Lock,
    destroyed_agents: list[AgentName],
    branches_to_remove: list[tuple[str, Path]],
    failures: list[CleanupFailure],
) -> None:
    """Destroy a single agent on an online host. Thread-safe."""
    agent_display = f"{agent.name}@{host.get_name()}"
    try:
        if agent.is_running() and not opts.force:
            _output(
                f"Agent {agent_display} is running. Use --force to destroy running agents.",
                output_opts,
            )
            return

        if opts.remove_created_branch:
            source_repo_path = find_source_repo_of_worktree(agent.work_dir)
            if source_repo_path is not None:
                created_branch = agent.get_created_branch_name()
                if created_branch is not None:
                    with results_lock:
                        branches_to_remove.append((created_branch, source_repo_path))

        mngr_ctx.pm.hook.on_before_agent_destroy(agent=agent, host=host)
        # destroy_agent raises a CleanupFailedGroup carrying the real failures (resources
        # left behind) rather than failing fast; we still acted, so record the agent.
        agent_failures: tuple[CleanupFailure, ...] = ()
        try:
            host.destroy_agent(agent)
        except CleanupFailedGroup as group:
            agent_failures = group.failures
        mngr_ctx.pm.hook.on_agent_destroyed(agent=agent, host=host)
        with results_lock:
            destroyed_agents.append(agent.name)
            failures.extend(agent_failures)
        _output(f"Destroyed agent: {agent_display}", output_opts)

        # Emit agent_destroyed event, then re-emit remaining host state
        emit_agent_destroyed(mngr_ctx.config, agent.id, host.id)
        emit_discovery_events_for_host(mngr_ctx.config, host)

    except MngrError as e:
        _output(f"Error destroying agent {agent_display}: {e}", output_opts)
        with results_lock:
            failures.append(
                CleanupFailure(
                    category=CleanupFailureCategory.OTHER,
                    message=f"Error destroying agent {agent_display}: {e}",
                    agent_name=agent.name,
                    host_id=host.id,
                )
            )


def _destroy_single_offline_host(
    offline: _OfflineHostToDestroy,
    output_opts: OutputOptions,
    mngr_ctx: MngrContext,
    results_lock: threading.Lock,
    destroyed_agents: list[AgentName],
    failures: list[CleanupFailure],
) -> None:
    """Destroy a single offline host and all its agents. Thread-safe."""
    host_name = offline.host.get_name()
    try:
        _output(f"Destroying offline host {host_name} with {len(offline.agent_names)} agent(s)...", output_opts)
        mngr_ctx.pm.hook.on_before_host_destroy(host=offline.host, mngr_ctx=mngr_ctx)
        # destroy_host raises a CleanupFailedGroup carrying the real failures (resources
        # left behind) rather than failing fast; we still acted, so record the agents.
        host_failures: tuple[CleanupFailure, ...] = ()
        try:
            offline.provider.destroy_host(offline.host)
        except CleanupFailedGroup as group:
            host_failures = group.failures
        mngr_ctx.pm.hook.on_host_destroyed(host=offline.host, mngr_ctx=mngr_ctx)
        with results_lock:
            destroyed_agents.extend(offline.agent_names)
            failures.extend(host_failures)
        for name in offline.agent_names:
            _output(f"Destroyed agent: {name}@{host_name} (via host destruction)", output_opts)

        # Emit host_destroyed event with all agent IDs
        emit_host_destroyed(mngr_ctx.config, offline.host.id, offline.agent_ids)
    except MngrError as e:
        _output(f"Error destroying offline host {host_name}: {e}", output_opts)
        with results_lock:
            failures.append(
                CleanupFailure(
                    category=CleanupFailureCategory.PROVIDER_INACCESSIBLE,
                    message=f"Error destroying offline host {host_name}: {e}",
                    host_id=offline.host.id,
                )
            )


def _check_all_agents_targeted_on_offline_host(
    offline_host: HostInterface,
    matched_ids: set[AgentId],
    host_id_str: str,
    offline_hosts: list[_OfflineHostToDestroy],
    provider: BaseProviderInstance,
    results_lock: threading.Lock,
) -> None:
    """Verify all agents on an offline host are targeted, then queue it for destruction.

    Offline hosts can only be destroyed as a whole -- individual agent destruction
    requires the host to be online. Raises HostOfflineError if only some agents
    are targeted.
    """
    all_agent_refs = offline_host.discover_agents()
    with results_lock:
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


def _emit_dry_run_output(targets: _DestroyTargets, output_opts: OutputOptions) -> None:
    """Report what would be destroyed without destroying anything.

    Collects the targeted agents (online and offline) into a flat list of
    entries and emits them via :func:`_emit_dry_run_entries`.
    """
    agent_entries: list[dict[str, str]] = []
    for agent, host in targets.online_agents:
        agent_entries.append({"name": str(agent.name), "host": host.get_name(), "offline": "false"})
    for offline in targets.offline_hosts:
        host_name = offline.host.get_name()
        for name in offline.agent_names:
            agent_entries.append({"name": str(name), "host": host_name, "offline": "true"})
    _emit_dry_run_entries(agent_entries, output_opts)


def _emit_dry_run_entries(agent_entries: Sequence[dict[str, str]], output_opts: OutputOptions) -> None:
    """Emit dry-run agent entries, honoring the active output format.

    Honors the same output formats as the real destroy result: format
    templates, JSON, JSONL, and human-readable. Offline-host agents are
    annotated as such in human output.
    """
    if output_opts.format_template is not None:
        emit_format_template_lines(output_opts.format_template, agent_entries)
        return

    result_data = {"dry_run": True, "agents": list(agent_entries), "count": len(agent_entries)}
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line(result_data)
        case OutputFormat.JSONL:
            emit_event("dry_run", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("\nWould destroy {} agent(s):", len(agent_entries))
            for entry in agent_entries:
                suffix = " (offline)" if entry["offline"] == "true" else ""
                write_human_line("  - {}@{}{}", entry["name"], entry["host"], suffix)
        case _ as unreachable:
            assert_never(unreachable)


def _confirm_destruction(targets: _DestroyTargets) -> None:
    """Prompt user to confirm destruction of agents."""
    write_human_line("\nThe following agents will be destroyed:")
    for agent, host in targets.online_agents:
        write_human_line("  - {}@{}", agent.name, host.get_name())
    for offline in targets.offline_hosts:
        host_name = offline.host.get_name()
        for name in offline.agent_names:
            write_human_line("  - {}@{} (offline)", name, host_name)

    write_human_line("\nThis action is irreversible!")

    if not click.confirm("Are you sure you want to continue?"):
        raise click.Abort()


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message according to the format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(
    destroyed_agents: Sequence[AgentName],
    failures: Sequence[CleanupFailure],
    output_opts: OutputOptions,
) -> None:
    """Output the final result, including any real cleanup failures."""
    if output_opts.format_template is not None:
        items = [{"name": str(n)} for n in destroyed_agents]
        emit_format_template_lines(output_opts.format_template, items)
        return
    result_data = {
        "destroyed_agents": [str(n) for n in destroyed_agents],
        "count": len(destroyed_agents),
        "failures": [failure.model_dump(mode="json") for failure in failures],
        "failure_count": len(failures),
        "exit_code": exit_code_for_failures(failures),
    }
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line(result_data)
        case OutputFormat.JSONL:
            emit_event("destroy_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if destroyed_agents:
                write_human_line("\nSuccessfully destroyed {} agent(s)", len(destroyed_agents))
            if failures:
                logger.warning("{} cleanup failure(s) -- resources may remain:", len(failures))
                for failure in failures:
                    logger.warning("  - [{}] {}", failure.category.value, failure.message)
        case _ as unreachable:
            assert_never(unreachable)


def _remove_created_branch(
    branch_name: str,
    source_repo_path: Path,
    cg: ConcurrencyGroup,
    output_opts: OutputOptions,
) -> None:
    """Delete a git branch from the source repository, with human-facing output.

    Called after the post-destroy GC pass, which is what removes the agent's
    worktree (when GC and work-dir cleanup are both enabled). If the worktree
    still has the branch checked out -- for example when --no-gc or
    --no-allow-worktree-removal was passed -- ``git branch -D`` will fail;
    such failures are logged as warnings and do not fail the destroy
    operation.
    """
    if delete_git_branch(branch_name, source_repo_path, cg):
        _output(f"Deleted branch: {branch_name}", output_opts)


def _destroy_emptied_hosts(
    online_hosts_with_provider: Sequence[tuple[OnlineHostInterface, ProviderInstanceInterface]],
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    results_lock: threading.Lock,
    failures: list[CleanupFailure],
) -> None:
    """Destroy each online host whose last live agent was just destroyed.

    Delivers the destroy CLI's documented "when the last agent on a host is
    destroyed, the host itself is destroyed" contract for all hosts -- not
    just those past ``gc_machines``'s ``min_online_host_age_seconds`` filter.
    Each host is re-checked via ``host.get_agents()`` after the per-agent
    destroy loop completed; only hosts that now have zero agents are
    destroyed. Hosts that retained one or more agents (e.g. only some of
    the host's agents were targeted) are left alone.

    Caller has already deduplicated by host id in
    ``_resolve_host_for_partition``, so one (host, provider) entry per host
    is guaranteed.

    This is a best-effort convenience pass, not the operation the user asked for
    (which was to destroy the agents -- already done). A host that *cannot* be
    destroyed here is therefore not a cleanup failure: the local host is never
    destroyable (``LocalHostNotDestroyableError``), and a transient connection /
    auth / provider error is logged and skipped (the post-destroy GC pass that
    runs immediately after is the safety net that retries once the failure
    clears). So a *raised* error from this sweep does not contribute to the
    command's exit code. A host that *was* destroyed but left a real resource
    behind is surfaced normally -- those failures come back as a
    ``CleanupFailedGroup`` raised by ``destroy_host``, which we catch and record.
    """
    for host, provider in online_hosts_with_provider:
        host_name = host.get_name()
        try:
            remaining = host.get_agents()
        except (HostConnectionError, HostAuthenticationError) as exc:
            logger.warning(
                "Cannot re-check host {} for emptiness after destroying its agents (skipping host destroy): {}",
                host_name,
                exc,
            )
            continue
        if remaining:
            logger.debug(
                "Host {} still has {} agent(s) after destroy; leaving host alive",
                host_name,
                len(remaining),
            )
            continue
        try:
            mngr_ctx.pm.hook.on_before_host_destroy(host=host, mngr_ctx=mngr_ctx)
            # destroy_host raises a CleanupFailedGroup carrying the real "destroyed but a
            # resource leaked" failures (not an MngrError), which we surface; an MngrError
            # means the destroy could not be attempted at all (handled below).
            host_failures: tuple[CleanupFailure, ...] = ()
            try:
                provider.destroy_host(host)
            except CleanupFailedGroup as group:
                host_failures = group.failures
            mngr_ctx.pm.hook.on_host_destroyed(host=host, mngr_ctx=mngr_ctx)
            emit_host_destroyed(mngr_ctx.config, host.id, [])
            _output(f"Destroyed empty host: {host_name}", output_opts)
            with results_lock:
                failures.extend(host_failures)
        except MngrError as exc:
            # Best-effort: this implicit host-destroy could not even be attempted (e.g. the
            # local host is not destroyable, or a transient provider error). The agent
            # destroy the user asked for succeeded, and GC is the safety net, so this is
            # logged and skipped rather than recorded as a cleanup failure.
            logger.warning("Skipping destroy of emptied host {} (GC will retry): {}", host_name, exc)


def _run_post_destroy_gc(
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
    include_work_dirs: bool,
) -> None:
    """Run garbage collection after destroying agents.

    This cleans up orphaned host-level resources (machines, work dirs, snapshots,
    volumes) and orphaned provider-level resources (e.g. Azure NICs/public IPs left
    by a failed create). Errors are logged but don't prevent destroy from reporting
    success.

    ``include_work_dirs`` follows the destroy command's --allow-worktree-removal
    flag.  When False, GC skips work-dir cleanup so the user's worktree stays
    on disk (other resources are still GC'd).
    """
    try:
        _output("Garbage collecting...", output_opts)

        providers = get_all_provider_instances(mngr_ctx)

        resource_types = GcResourceTypes(
            is_machines=True,
            is_work_dirs=include_work_dirs,
            is_snapshots=True,
            is_volumes=True,
            is_logs=False,
            is_build_cache=False,
            is_provider_resources=True,
        )

        result = api_gc(
            mngr_ctx=mngr_ctx,
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

    except MngrError as e:
        logger.warning("Garbage collection failed: {}", e)
        logger.warning("This does not affect the destroy operation, which completed successfully")


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="destroy",
    one_line_description="Destroy agent(s) and clean up resources",
    synopsis="mngr [destroy|rm] [AGENTS...|-] [--agent <AGENT>] [--session <SESSION>] [-f|--force] [-b|--remove-created-branch] [--[no-]gc] [--[no-]allow-worktree-removal] [--dry-run]",
    description="""When the last agent on a host is destroyed, the host itself is also destroyed
(including containers, volumes, snapshots, and any remote infrastructure).

Use with caution! This operation is irreversible.

By default, running agents cannot be destroyed. Use --force to stop and destroy
running agents. The command will prompt for confirmation before destroying
agents unless --force is specified.

Use '-' in place of agent names to read them from stdin, one per line.

Supports custom format templates via --format. Available fields: name.""",
    aliases=("rm",),
    examples=(
        ("Destroy an agent by name", "mngr destroy my-agent"),
        ("Destroy multiple agents", "mngr destroy agent1 agent2 agent3"),
        ("Destroy all agents", "mngr list --ids | mngr destroy - --force"),
        ("Destroy using --agent flag (repeatable)", "mngr destroy --agent my-agent --agent another-agent"),
        ("Destroy by tmux session name", "mngr destroy --session mngr-my-agent"),
        ("Pipe agent names from list", "mngr list --ids | mngr destroy - --force"),
        ("Preview what would be destroyed", "mngr list --ids | mngr destroy - --dry-run"),
        ("Custom format template output", "mngr destroy my-agent --force --format '{name}'"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List existing agents"),
        ("gc", "Garbage collect orphaned resources"),
        ("resource_cleanup", "Control which associated resources are destroyed"),
        ("multi_target", "Behavior when targeting multiple agents"),
    ),
).register()

# Add pager-enabled help option to the destroy command
add_pager_help_option(destroy)
