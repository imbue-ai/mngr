from collections.abc import Sequence
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import HOST_ADDRESS
from imbue.mngr.cli.address_params import parse_agent_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import HostResizeNotSupportedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import ActivityConfig
from imbue.mngr.interfaces.data_types import HostResizeDimensionCapability
from imbue.mngr.interfaces.data_types import HostResizeRequest
from imbue.mngr.interfaces.data_types import HostResizeValue
from imbue.mngr.interfaces.data_types import get_activity_sources_for_idle_mode
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.duration import parse_duration_to_seconds


class LimitCliOptions(CommonCliOptions):
    """Options passed from the CLI to the limit command."""

    agents: tuple[str, ...]
    agent_list: tuple[AgentAddress, ...]
    hosts: tuple[HostAddress, ...]
    # Lifecycle
    start_on_boot: bool | None
    idle_timeout: str | None
    idle_mode: str | None
    activity_sources: str | None
    add_activity_source: tuple[str, ...]
    remove_activity_source: tuple[str, ...]
    # Resources (positive integer or 'default')
    cpus: str | None
    memory: str | None
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
            write_json_line(result_data)
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


def _has_host_level_settings(opts: LimitCliOptions) -> bool:
    """Return True if any host-level activity settings are being changed."""
    return (
        opts.idle_timeout is not None
        or opts.idle_mode is not None
        or opts.activity_sources is not None
        or len(opts.add_activity_source) > 0
        or len(opts.remove_activity_source) > 0
    )


def _has_resource_settings(opts: LimitCliOptions) -> bool:
    """Return True if any host resource limits (CPU/memory) are being changed."""
    return opts.cpus is not None or opts.memory is not None


def _has_agent_level_settings(opts: LimitCliOptions) -> bool:
    """Return True if any agent-level settings are being changed."""
    return opts.start_on_boot is not None


def _has_any_setting(opts: LimitCliOptions) -> bool:
    """Return True if any setting is being changed."""
    return _has_host_level_settings(opts) or _has_agent_level_settings(opts) or _has_resource_settings(opts)


@pure
def _parse_resource_flag_value(raw: str | None, flag_name: str) -> int | str | None:
    """Parse a --cpus/--memory flag into a positive integer or the literal 'default'.

    Raises click.UsageError for anything else.
    """
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized == "default":
        return "default"
    try:
        parsed_value = int(normalized)
    except ValueError:
        raise click.UsageError(f"{flag_name} must be a positive integer or 'default', got {raw!r}") from None
    if parsed_value < 1:
        raise click.UsageError(f"{flag_name} must be at least 1, got {raw!r}")
    return parsed_value


def _build_resize_value(
    parsed_flag: int | str,
    dimension: HostResizeDimensionCapability | None,
    dimension_name: str,
    provider_name: str,
) -> HostResizeValue:
    """Resolve a parsed --cpus/--memory flag against a provider's capability descriptor.

    'default' resolves to the provider's default value (which may be a clear-to-unlimited
    for providers whose default is no limit). A concrete value above the physical ceiling
    warns but proceeds -- over-provisioning is allowed, never blocked.
    """
    if dimension is None:
        raise UserInputError(f"Provider {provider_name} does not support resizing {dimension_name}")
    if isinstance(parsed_flag, str):
        return HostResizeValue(value=dimension.default_value)
    if dimension.ceiling is not None and parsed_flag > dimension.ceiling:
        logger.warning(
            "Requested {}={} exceeds the physical ceiling of {}; over-provisioning is allowed but may "
            "degrade performance",
            dimension_name,
            parsed_flag,
            dimension.ceiling,
        )
    return HostResizeValue(value=parsed_flag)


def _apply_resource_changes(
    provider: ProviderInstanceInterface,
    host_id: HostId,
    opts: LimitCliOptions,
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
) -> None:
    """Set the requested CPU/memory limits on a host via its provider and record the outcome.

    Works for stopped hosts too: the provider persists the values and they apply
    on the host's next start (visible as a configured/actual discrepancy).
    """
    parsed_cpus = _parse_resource_flag_value(opts.cpus, "--cpus")
    parsed_memory = _parse_resource_flag_value(opts.memory, "--memory")

    capabilities = provider.get_resize_capabilities()
    if not capabilities.is_resize_supported:
        raise HostResizeNotSupportedError(provider.name)

    resize_request = HostResizeRequest(
        cpu_count=_build_resize_value(parsed_cpus, capabilities.cpu, "cpus", str(provider.name))
        if parsed_cpus is not None
        else None,
        memory_gib=_build_resize_value(parsed_memory, capabilities.memory_gib, "memory", str(provider.name))
        if parsed_memory is not None
        else None,
    )
    report = provider.resize_host(host_id, resize_request)

    configured = report.configured
    _output(
        f"Set resources for host {host_id}: cpus={_format_limit(configured.cpu_count)} "
        f"memory={_format_limit(configured.memory_gib)}GiB",
        output_opts,
    )
    if report.actual is not None and report.actual != configured:
        _output(
            f"Host {host_id} is running with cpus={_format_limit(report.actual.cpu_count)} "
            f"memory={_format_limit(report.actual.memory_gib)}GiB; the new values apply on its next restart",
            output_opts,
        )
    changes.append(
        {
            "type": "host_resources",
            "host_id": str(host_id),
            "configured": configured.model_dump(mode="json"),
            "actual": report.actual.model_dump(mode="json") if report.actual is not None else None,
        }
    )


@pure
def _format_limit(value: float | None) -> str:
    """Format a limit value for human output ('none' when unlimited)."""
    if value is None:
        return "none"
    return f"{value:g}"


def _build_host_limits_entry(
    provider: ProviderInstanceInterface,
    host_id: HostId,
    host_name: str,
) -> dict[str, Any]:
    """Build the read-mode report entry for one host: capabilities + configured + actual."""
    capabilities = provider.get_resize_capabilities()
    entry: dict[str, Any] = {
        "host_id": str(host_id),
        "host_name": host_name,
        "provider": str(provider.name),
        "capabilities": capabilities.model_dump(mode="json"),
        "configured": None,
        "actual": None,
    }
    if capabilities.is_resize_supported:
        report = provider.get_host_resource_limits(host_id)
        entry["configured"] = report.configured.model_dump(mode="json")
        entry["actual"] = report.actual.model_dump(mode="json") if report.actual is not None else None
    return entry


def _resolve_distinct_target_hosts(
    opts: LimitCliOptions,
    agent_addresses: Sequence[AgentAddress],
    mngr_ctx: MngrContext,
) -> list[tuple[ProviderInstanceName, HostId, str]]:
    """Resolve the targeted agents/hosts to a deduplicated list of (provider, host_id, host_name)."""
    if opts.hosts and not agent_addresses:
        all_hosts = _build_host_references(mngr_ctx)
        resolved_hosts = [filter_one_host(host_address, all_hosts) for host_address in opts.hosts]
        return [(h.provider_name, h.host_id, str(h.host_name)) for h in resolved_hosts]

    agents = find_all_agents(
        addresses=list(agent_addresses),
        filter_all=False,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )
    if opts.hosts:
        resolved_host_ids = _resolve_host_addresses(opts.hosts, mngr_ctx)
        agents = [a for a in agents if a.host_id in resolved_host_ids]

    distinct_hosts: list[tuple[ProviderInstanceName, HostId, str]] = []
    seen_host_ids: set[HostId] = set()
    for agent in agents:
        if agent.host_id in seen_host_ids:
            continue
        seen_host_ids.add(agent.host_id)
        distinct_hosts.append((agent.provider_name, agent.host_id, str(agent.host_name)))
    return distinct_hosts


def _output_limits_report(entries: list[dict[str, Any]], output_opts: OutputOptions) -> None:
    """Output the read-mode resource limits report."""
    result_data = {"hosts": entries, "count": len(entries)}
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line(result_data)
        case OutputFormat.JSONL:
            emit_event("limit_report", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            for entry in entries:
                configured = entry["configured"]
                actual = entry["actual"]
                if configured is None:
                    write_human_line(
                        "host {} ({}): resource resizing not supported", entry["host_name"], entry["provider"]
                    )
                    continue
                configured_text = f"cpus={_format_limit(configured['cpu_count'])} memory={_format_limit(configured['memory_gib'])}GiB"
                if actual is None:
                    write_human_line(
                        "host {} ({}): configured {} (not running)",
                        entry["host_name"],
                        entry["provider"],
                        configured_text,
                    )
                else:
                    actual_text = (
                        f"cpus={_format_limit(actual['cpu_count'])} memory={_format_limit(actual['memory_gib'])}GiB"
                    )
                    if actual == configured:
                        write_human_line("host {} ({}): {}", entry["host_name"], entry["provider"], configured_text)
                    else:
                        write_human_line(
                            "host {} ({}): configured {}; running with {} (configured values apply on next restart)",
                            entry["host_name"],
                            entry["provider"],
                            configured_text,
                            actual_text,
                        )
        case _ as unreachable:
            assert_never(unreachable)


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


def _resolve_host_addresses(
    host_addresses: Sequence[HostAddress],
    mngr_ctx: MngrContext,
) -> set[HostId]:
    """Resolve a sequence of :class:`HostAddress` to a set of :class:`HostId`.

    Raises :class:`UserInputError` if any host address cannot be resolved.
    """
    all_hosts = _build_host_references(mngr_ctx)
    resolved_ids: set[HostId] = set()
    for host_address in host_addresses:
        resolved_host = filter_one_host(host_address, all_hosts)
        resolved_ids.add(resolved_host.host_id)
    return resolved_ids


@click.command(name="limit")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    type=AGENT_ADDRESS,
    multiple=True,
    help="Agent address (NAME[@HOST[.PROVIDER]]) to configure (can be specified multiple times)",
)
@optgroup.option(
    "--host",
    "hosts",
    type=HOST_ADDRESS,
    multiple=True,
    help="Host address (HOST[.PROVIDER]) to configure (can be specified multiple times)",
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
@optgroup.group("Resources")
@optgroup.option(
    "--cpus",
    type=str,
    default=None,
    help="Set the host's CPU allotment (positive integer, or 'default' for the provider default)",
)
@optgroup.option(
    "--memory",
    type=str,
    default=None,
    help="Set the host's memory allotment in GiB (positive integer, or 'default' for the provider default)",
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
    if opts.refresh_ssh_keys:
        raise NotImplementedError("--refresh-ssh-keys is not implemented yet")
    if opts.add_ssh_key:
        raise NotImplementedError("--add-ssh-key is not implemented yet")
    if opts.remove_ssh_key:
        raise NotImplementedError("--remove-ssh-key is not implemented yet")

    # Fail fast on malformed resource flags, before any discovery work
    _parse_resource_flag_value(opts.cpus, "--cpus")
    _parse_resource_flag_value(opts.memory, "--memory")

    # Validate --activity-sources is not combined with --add/--remove-activity-source
    if opts.activity_sources is not None and (opts.add_activity_source or opts.remove_activity_source):
        raise click.UsageError(
            "Cannot combine --activity-sources with --add-activity-source or --remove-activity-source"
        )

    # Validate targets: must specify agents or --host
    agent_addresses: list[AgentAddress] = parse_agent_addresses_or_raise(expand_stdin_placeholder(opts.agents)) + list(
        opts.agent_list
    )
    has_agents = bool(agent_addresses)
    has_hosts = bool(opts.hosts)

    if not has_agents and not has_hosts:
        if STDIN_PLACEHOLDER not in opts.agents:
            raise click.UsageError(
                "Must specify at least one agent or --host (use '-' to read agent names from stdin)"
            )
        return

    # With no settings to change, report the targets' resize capabilities and
    # configured/actual resource limits instead
    if not _has_any_setting(opts):
        report_entries = [
            _build_host_limits_entry(get_provider_instance(provider_name, mngr_ctx), host_id, host_name)
            for provider_name, host_id, host_name in _resolve_distinct_target_hosts(opts, agent_addresses, mngr_ctx)
        ]
        _output_limits_report(report_entries, output_opts)
        return

    # If only --host is specified (no agents), agent-level settings are not allowed
    if has_hosts and not has_agents and _has_agent_level_settings(opts):
        raise click.UsageError(
            "Agent-level settings (--start-on-boot) require agent targeting. "
            "Use --agent or positional args with --host to target agents on specific hosts."
        )

    # If --host only (no agents), apply host-level changes directly
    if has_hosts and not has_agents:
        changes: list[dict[str, Any]] = []
        all_hosts = _build_host_references(mngr_ctx)
        for host_address in opts.hosts:
            _apply_host_only_changes(
                host_address=host_address,
                all_hosts=all_hosts,
                opts=opts,
                output_opts=output_opts,
                mngr_ctx=mngr_ctx,
                changes=changes,
            )
        _output_result(changes, output_opts)
        return

    # Find agents (match all states for limit command)
    agents = find_all_agents(
        addresses=agent_addresses,
        filter_all=False,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )

    if not agents:
        _output("No agents found to configure", output_opts)
        return

    # If --host is also specified, filter agents to those on the specified hosts
    if has_hosts:
        resolved_host_ids = _resolve_host_addresses(opts.hosts, mngr_ctx)
        target_agents = [a for a in agents if a.host_id in resolved_host_ids]
        if not target_agents:
            _output("No agents found on the specified host(s)", output_opts)
            return
    else:
        target_agents = agents

    # Apply changes
    changes = []
    agents_by_host = group_agents_by_host(target_agents)
    updated_host_ids: set[str] = set()

    resized_host_ids: set[str] = set()
    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)

        # Resource limits go through the provider directly (not the online host),
        # so they can be set on stopped hosts too and apply on the next start.
        if _has_resource_settings(opts) and host_id_str not in resized_host_ids:
            _apply_resource_changes(
                provider=provider,
                host_id=HostId(host_id_str),
                opts=opts,
                output_opts=output_opts,
                changes=changes,
            )
            resized_host_ids.add(host_id_str)

        if not _has_host_level_settings(opts) and not _has_agent_level_settings(opts):
            continue

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

    _output_result(changes, output_opts)


def _apply_host_only_changes(
    host_address: HostAddress,
    all_hosts: list[DiscoveredHost],
    opts: LimitCliOptions,
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
    mngr_ctx: MngrContext,
) -> None:
    """Apply host-level changes when targeting hosts directly (no agents).

    Raises UserInputError if the host address cannot be resolved.
    """
    resolved_host = filter_one_host(host_address, all_hosts)

    provider = get_provider_instance(resolved_host.provider_name, mngr_ctx)

    # Resource limits go through the provider directly (not the online host),
    # so they can be set on stopped hosts too and apply on the next start.
    if _has_resource_settings(opts):
        _apply_resource_changes(
            provider=provider,
            host_id=resolved_host.host_id,
            opts=opts,
            output_opts=output_opts,
            changes=changes,
        )

    if not _has_host_level_settings(opts):
        return

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

            break
    else:
        raise AgentNotFoundOnHostError(agent_match.agent_id, agent_match.host_id)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="limit",
    one_line_description="Configure limits for agents and hosts [experimental]",
    synopsis="mngr [limit|lim] [AGENTS...|-] [--agent <AGENT>] [--host <HOST>] [--cpus <N|default>] [--memory <GIB|default>] [--idle-timeout <DURATION>] [--idle-mode <MODE>] [--start-on-boot|--no-start-on-boot]",
    arguments_description="- `AGENTS`: Agent name(s) or ID(s) to configure (can also be specified via `--agent`)",
    description="""When targeting agents, host-level settings (cpus, memory, idle-timeout,
idle-mode, activity-sources) are applied to each agent's underlying host.

Resource limits (--cpus, --memory) are handled by the provider (currently
lima and docker). Setting them never restarts the host: values that cannot
apply live are persisted and take effect on the host's next restart, which
shows up as a difference between the configured and actual values in the
output. Pass 'default' to restore the provider's default. Values above the
machine's physical resources print a warning but are allowed.

With no settings to change, reports the targets' resize capabilities and
their configured and actual resource values (use --format json for a
machine-readable report).

Agent-level settings (start-on-boot) require agent targeting
and cannot be used with --host alone.

Use '-' in place of agent names to read them from stdin, one per line.""",
    aliases=("lim",),
    examples=(
        ("Set idle timeout for an agent's host", "mngr limit my-agent --idle-timeout 5m"),
        ("Disable idle detection for all agents", "mngr list --ids | mngr limit - --idle-mode disabled"),
        ("Update host idle settings directly", "mngr limit --host my-host --idle-timeout 1h"),
        ("Give an agent's host 8 CPUs and 16 GiB of memory", "mngr limit my-agent --cpus 8 --memory 16"),
        ("Restore a host's default resources", "mngr limit --host my-host --cpus default --memory default"),
        ("Report configured and actual resources", "mngr limit --host my-host --format json"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("list", "List existing agents"),
        ("stop", "Stop running agents"),
        ("idle_detection", "Idle detection modes and activity sources"),
    ),
).register()

add_pager_help_option(limit)
