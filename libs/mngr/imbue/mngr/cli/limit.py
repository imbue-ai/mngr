from collections.abc import Sequence
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.find import resolve_host_reference
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.agent_addr import find_agents_by_addresses
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.interfaces.data_types import ActivityConfig
from imbue.mngr.interfaces.data_types import get_activity_sources_for_idle_mode
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import Permission
from imbue.mngr.utils.duration import parse_duration_to_seconds


class LimitCliOptions(CommonCliOptions):
    """Options passed from the CLI to the limit command."""

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    hosts: tuple[str, ...]
    limit_all: bool
    dry_run: bool
    # Planned features (not yet implemented)
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    # Lifecycle
    start_on_boot: bool | None
    idle_timeout: str | None
    idle_mode: str | None
    activity_sources: str | None
    add_activity_source: tuple[str, ...]
    remove_activity_source: tuple[str, ...]
    # Permissions
    grant: tuple[str, ...]
    revoke: tuple[str, ...]
    # SSH Keys (not yet implemented)
    refresh_ssh_keys: bool
    add_ssh_key: tuple[str, ...]
    remove_ssh_key: tuple[str, ...]


def _make_idle_mode_choices() -> list[str]:
    """Get lowercase idle mode choices (excluding CUSTOM, which is derived, not user-settable)."""
    return [m.value.lower() for m in IdleMode if m != IdleMode.CUSTOM]


def _make_activity_source_choices() -> list[str]:
    """Get lowercase activity source choices."""
    return [s.value.lower() for s in ActivitySource]


def _output(message: str, output_opts: OutputOptions) -> None:
    """Output a message according to the format."""
    if output_opts.output_format == OutputFormat.HUMAN:
        write_human_line(message)


def _output_result(
    changes: list[dict[str, Any]],
    output_opts: OutputOptions,
) -> None:
    """Output the final result."""
    result_data = {"changes": changes, "count": len(changes)}
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("limit_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if changes:
                write_human_line("Applied {} change(s)", len(changes))
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _build_updated_activity_config(
    current: ActivityConfig,
    idle_timeout_str: str | None,
    idle_mode_str: str | None,
    activity_sources_str: str | None,
    add_activity_source: tuple[str, ...],
    remove_activity_source: tuple[str, ...],
) -> ActivityConfig:
    """Build an updated ActivityConfig by merging current config with requested changes.

    idle_mode is a computed property on ActivityConfig (derived from activity_sources),
    so when --idle-mode is specified we convert it to the corresponding activity sources
    via get_activity_sources_for_idle_mode.
    """
    new_idle_timeout = (
        int(parse_duration_to_seconds(idle_timeout_str))
        if idle_timeout_str is not None
        else current.idle_timeout_seconds
    )

    if activity_sources_str is not None:
        # Explicit --activity-sources replaces everything
        new_activity_sources = tuple(ActivitySource(s.strip().upper()) for s in activity_sources_str.split(","))
    elif idle_mode_str is not None:
        # --idle-mode sets the canonical activity sources for that mode
        new_activity_sources = get_activity_sources_for_idle_mode(IdleMode(idle_mode_str.upper()))
    else:
        # Incremental changes via --add/--remove-activity-source
        current_sources = set(current.activity_sources)
        for source_str in add_activity_source:
            current_sources.add(ActivitySource(source_str.upper()))
        for source_str in remove_activity_source:
            current_sources.discard(ActivitySource(source_str.upper()))
        new_activity_sources = tuple(current_sources)

    return ActivityConfig(
        idle_timeout_seconds=new_idle_timeout,
        activity_sources=new_activity_sources,
    )


@pure
def _build_updated_permissions(
    current: Sequence[Permission],
    grant: tuple[str, ...],
    revoke: tuple[str, ...],
) -> list[Permission]:
    """Build an updated permissions list by applying grants and revokes."""
    result = set(current)
    for perm_str in grant:
        result.add(Permission(perm_str))
    for perm_str in revoke:
        result.discard(Permission(perm_str))
    return sorted(result)


def _has_host_level_settings(opts: LimitCliOptions) -> bool:
    """Return True if any host-level settings are being changed."""
    return (
        opts.idle_timeout is not None
        or opts.idle_mode is not None
        or opts.activity_sources is not None
        or len(opts.add_activity_source) > 0
        or len(opts.remove_activity_source) > 0
    )


def _has_agent_level_settings(opts: LimitCliOptions) -> bool:
    """Return True if any agent-level settings are being changed."""
    return opts.start_on_boot is not None or len(opts.grant) > 0 or len(opts.revoke) > 0


def _has_any_setting(opts: LimitCliOptions) -> bool:
    """Return True if any setting is being changed."""
    return _has_host_level_settings(opts) or _has_agent_level_settings(opts)


def _apply_activity_config_to_host(
    online_host: OnlineHostInterface,
    host_id_str: str,
    opts: LimitCliOptions,
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
) -> None:
    """Apply activity config changes to a single online host."""
    current_config = online_host.get_activity_config()
    new_config = _build_updated_activity_config(
        current=current_config,
        idle_timeout_str=opts.idle_timeout,
        idle_mode_str=opts.idle_mode,
        activity_sources_str=opts.activity_sources,
        add_activity_source=opts.add_activity_source,
        remove_activity_source=opts.remove_activity_source,
    )
    online_host.set_activity_config(new_config)
    _output(f"Updated activity config for host {host_id_str}", output_opts)
    changes.append(
        {
            "type": "host_activity_config",
            "host_id": host_id_str,
        }
    )


def _build_host_references(mngr_ctx: MngrContext) -> list[DiscoveredHost]:
    """Build a deduplicated list of DiscoveredHosts from all known agents."""
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    return list(agents_by_host.keys())


def _resolve_host_identifiers(
    host_identifiers: tuple[str, ...],
    mngr_ctx: MngrContext,
) -> set[HostId]:
    """Resolve host identifiers (names or IDs) to a set of HostIds.

    Raises UserInputError if any host identifier cannot be resolved.
    """
    all_hosts = _build_host_references(mngr_ctx)
    resolved_ids: set[HostId] = set()
    for host_identifier in host_identifiers:
        # resolve_host_reference raises UserInputError for unresolvable identifiers;
        # it only returns None when host_identifier is None, which cannot happen here
        resolved_host = resolve_host_reference(host_identifier, all_hosts)
        assert resolved_host is not None
        resolved_ids.add(resolved_host.host_id)
    return resolved_ids


@click.command(name="limit")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to configure (can be specified multiple times)",
)
@optgroup.option(
    "--host",
    "hosts",
    multiple=True,
    help="Host name or ID to configure (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "limit_all",
    is_flag=True,
    help="Apply limits to all agents",
)
@optgroup.option(
    "--include",
    multiple=True,
    help="Filter agents to configure by CEL expression (repeatable) [future]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude agents matching CEL expression (repeatable) [future]",
)
@optgroup.group("Behavior")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what limits would be changed without actually changing them",
)
@optgroup.group("Lifecycle")
@optgroup.option(
    "--start-on-boot/--no-start-on-boot",
    default=None,
    help="Automatically restart agent when host restarts",
)
@optgroup.option(
    "--idle-timeout",
    type=str,
    default=None,
    help="Shutdown after idle for specified duration (e.g., 30s, 5m, 1h, or plain seconds)",
)
@optgroup.option(
    "--idle-mode",
    type=click.Choice(_make_idle_mode_choices(), case_sensitive=False),
    default=None,
    help="When to consider host idle",
)
@optgroup.option(
    "--activity-sources",
    type=str,
    default=None,
    help="Set activity sources for idle detection (comma-separated)",
)
@optgroup.option(
    "--add-activity-source",
    type=click.Choice(_make_activity_source_choices(), case_sensitive=False),
    multiple=True,
    help="Add an activity source for idle detection (repeatable)",
)
@optgroup.option(
    "--remove-activity-source",
    type=click.Choice(_make_activity_source_choices(), case_sensitive=False),
    multiple=True,
    help="Remove an activity source from idle detection (repeatable)",
)
@optgroup.group("Permissions")
@optgroup.option(
    "--grant",
    multiple=True,
    help="Grant a permission to the agent (repeatable)",
)
@optgroup.option(
    "--revoke",
    multiple=True,
    help="Revoke a permission from the agent (repeatable)",
)
@optgroup.group("SSH Keys")
@optgroup.option(
    "--refresh-ssh-keys",
    is_flag=True,
    help="Refresh the SSH keys for the host [future]",
)
@optgroup.option(
    "--add-ssh-key",
    multiple=True,
    help="Add an SSH public key to the host for access (repeatable) [future]",
)
@optgroup.option(
    "--remove-ssh-key",
    multiple=True,
    help="Remove an SSH public key from the host (repeatable) [future]",
)
@add_common_options
@click.pass_context
def limit(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="limit",
        command_class=LimitCliOptions,
    )
    logger.debug("Started limit command")

    # Check for unsupported [future] options
    if opts.include:
        raise NotImplementedError("--include is not implemented yet")
    if opts.exclude:
        raise NotImplementedError("--exclude is not implemented yet")
    if opts.refresh_ssh_keys:
        raise NotImplementedError("--refresh-ssh-keys is not implemented yet")
    if opts.add_ssh_key:
        raise NotImplementedError("--add-ssh-key is not implemented yet")
    if opts.remove_ssh_key:
        raise NotImplementedError("--remove-ssh-key is not implemented yet")

    # Validate at least one setting is being changed
    if not _has_any_setting(opts):
        raise click.UsageError(
            "Must specify at least one setting to change (e.g., --idle-timeout, --idle-mode, "
            "--activity-sources, --start-on-boot, --grant, --revoke)"
        )

    # Validate --activity-sources is not combined with --add/--remove-activity-source
    if opts.activity_sources is not None and (opts.add_activity_source or opts.remove_activity_source):
        raise click.UsageError(
            "Cannot combine --activity-sources with --add-activity-source or --remove-activity-source"
        )

    # Validate targets: must specify agents, --host, or --all
    agent_identifiers = expand_stdin_placeholder(opts.agents) + list(opts.agent_list)
    has_agents = bool(agent_identifiers)
    has_hosts = bool(opts.hosts)

    if not has_agents and not has_hosts and not opts.limit_all:
        raise click.UsageError("Must specify at least one agent, --host, or --all")

    if has_agents and opts.limit_all:
        raise click.UsageError("Cannot specify both agent names and --all")

    # If only --host is specified (no agents), agent-level settings are not allowed
    if has_hosts and not has_agents and not opts.limit_all and _has_agent_level_settings(opts):
        raise click.UsageError(
            "Agent-level settings (--start-on-boot, --grant, --revoke) require agent targeting. "
            "Use --agent or --all with --host to target agents on specific hosts."
        )

    # If --host only (no agents, no --all), apply host-level changes directly
    if has_hosts and not has_agents and not opts.limit_all:
        changes: list[dict[str, Any]] = []
        all_hosts = _build_host_references(mngr_ctx)
        for host_identifier in opts.hosts:
            _apply_host_only_changes(
                host_identifier=host_identifier,
                all_hosts=all_hosts,
                opts=opts,
                output_opts=output_opts,
                mngr_ctx=mngr_ctx,
                dry_run=opts.dry_run,
                changes=changes,
            )
        _output_result(changes, output_opts)
        return

    # Find agents (match all states for limit command)
    agents = find_agents_by_addresses(
        raw_identifiers=agent_identifiers,
        filter_all=opts.limit_all,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )

    if not agents:
        _output("No agents found to configure", output_opts)
        return

    # If --host is also specified, filter agents to those on the specified hosts
    if has_hosts:
        resolved_host_ids = _resolve_host_identifiers(opts.hosts, mngr_ctx)
        target_agents = [a for a in agents if a.host_id in resolved_host_ids]
        if not target_agents:
            _output("No agents found on the specified host(s)", output_opts)
            return
    else:
        target_agents = agents

    # Handle dry-run mode
    if opts.dry_run:
        _output("Would configure:", output_opts)
        for match in target_agents:
            _output(f"  - {match.agent_name} (on host {match.host_id})", output_opts)
        if _has_host_level_settings(opts):
            unique_hosts = {str(m.host_id) for m in target_agents}
            _output(f"Host-level changes would apply to {len(unique_hosts)} host(s)", output_opts)
        return

    # Apply changes
    changes = []
    agents_by_host = group_agents_by_host(target_agents)
    updated_host_ids: set[str] = set()
    has_permission_changes = bool(opts.grant or opts.revoke)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))

        match host:
            case OnlineHostInterface() as online_host:
                # Apply host-level changes once per host
                if _has_host_level_settings(opts) and host_id_str not in updated_host_ids:
                    _apply_activity_config_to_host(
                        online_host=online_host,
                        host_id_str=host_id_str,
                        opts=opts,
                        output_opts=output_opts,
                        changes=changes,
                    )
                    updated_host_ids.add(host_id_str)

                # Apply agent-level changes per agent
                if _has_agent_level_settings(opts):
                    for agent_match in agent_list:
                        _apply_agent_changes(
                            agent_match=agent_match,
                            online_host=online_host,
                            opts=opts,
                            output_opts=output_opts,
                            changes=changes,
                        )

            case HostInterface():
                raise HostOfflineError(f"Host '{host_id_str}' is offline. Cannot configure agents on offline hosts.")
            case _ as unreachable:
                assert_never(unreachable)

    if has_permission_changes:
        _output("Restart required for permission changes to take effect.", output_opts)

    _output_result(changes, output_opts)


def _apply_host_only_changes(
    host_identifier: str,
    all_hosts: list[DiscoveredHost],
    opts: LimitCliOptions,
    output_opts: OutputOptions,
    dry_run: bool,
    changes: list[dict[str, Any]],
    mngr_ctx: MngrContext,
) -> None:
    """Apply host-level changes when targeting hosts directly (no agents).

    Raises UserInputError if the host identifier cannot be resolved.
    """
    # resolve_host_reference raises UserInputError for unresolvable identifiers;
    # it only returns None when host_identifier is None, which cannot happen here
    resolved_host = resolve_host_reference(host_identifier, all_hosts)
    assert resolved_host is not None

    if dry_run:
        _output(f"Would update activity config for host {resolved_host.host_id}", output_opts)
        return

    provider = get_provider_instance(resolved_host.provider_name, mngr_ctx)
    host = provider.get_host(resolved_host.host_id)

    match host:
        case OnlineHostInterface() as online_host:
            _apply_activity_config_to_host(
                online_host=online_host,
                host_id_str=str(resolved_host.host_id),
                opts=opts,
                output_opts=output_opts,
                changes=changes,
            )
        case HostInterface():
            raise HostOfflineError(f"Host '{resolved_host.host_id}' is offline. Cannot configure offline hosts.")
        case _ as unreachable:
            assert_never(unreachable)


def _apply_agent_changes(
    agent_match: AgentMatch,
    online_host: OnlineHostInterface,
    opts: LimitCliOptions,
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
) -> None:
    """Apply agent-level changes to a single agent."""
    for agent in online_host.get_agents():
        if agent.id == agent_match.agent_id:
            if opts.start_on_boot is not None:
                agent.set_is_start_on_boot(opts.start_on_boot)
                _output(
                    f"Set start-on-boot={opts.start_on_boot} for agent {agent_match.agent_name}",
                    output_opts,
                )
                changes.append(
                    {
                        "type": "agent_start_on_boot",
                        "agent_id": str(agent_match.agent_id),
                        "agent_name": str(agent_match.agent_name),
                        "start_on_boot": opts.start_on_boot,
                    }
                )

            if opts.grant or opts.revoke:
                current_permissions = agent.get_permissions()
                new_permissions = _build_updated_permissions(
                    current=current_permissions,
                    grant=opts.grant,
                    revoke=opts.revoke,
                )
                agent.set_permissions(new_permissions)
                _output(
                    f"Updated permissions for agent {agent_match.agent_name}",
                    output_opts,
                )
                changes.append(
                    {
                        "type": "agent_permissions",
                        "agent_id": str(agent_match.agent_id),
                        "agent_name": str(agent_match.agent_name),
                        "permissions": [str(p) for p in new_permissions],
                    }
                )
            break
    else:
        raise AgentNotFoundOnHostError(agent_match.agent_id, agent_match.host_id)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="limit",
    one_line_description="Configure limits for agents and hosts [experimental]",
    synopsis="mngr [limit|lim] [AGENTS...|-] [--agent <AGENT>] [--host <HOST>] [--all] [--idle-timeout <DURATION>] [--idle-mode <MODE>] [--grant <PERM>] [--revoke <PERM>]",
    arguments_description="- `AGENTS`: Agent name(s) or ID(s) to configure (can also be specified via `--agent`)",
    description="""Agents effectively have permissions that are equivalent to the *union* of all
permissions on the same host. Changing permissions for agents requires them
to be restarted.

Changes to some limits for hosts (e.g. CPU, RAM, disk space, network) are
handled by the provider.

When targeting agents, host-level settings (idle-timeout, idle-mode,
activity-sources) are applied to each agent's underlying host.

Agent-level settings (start-on-boot, grant, revoke) require agent targeting
and cannot be used with --host alone.""",
    aliases=("lim",),
    examples=(
        ("Set idle timeout for an agent's host", "mngr limit my-agent --idle-timeout 5m"),
        ("Grant permissions to an agent", "mngr limit my-agent --grant network --grant internet"),
        ("Disable idle detection for all agents", "mngr limit --all --idle-mode disabled"),
        ("Update host idle settings directly", "mngr limit --host my-host --idle-timeout 1h"),
        ("Preview changes without applying", "mngr limit --all --idle-timeout 5m --dry-run"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List existing agents"),
        ("stop", "Stop running agents"),
    ),
    additional_sections=(
        (
            "Idle Modes",
            "See [Idle Detection](../../concepts/idle_detection.md) for details on idle modes and activity sources.",
        ),
    ),
).register()

add_pager_help_option(limit)
