import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mng.api.discover import discover_all_hosts_and_agents
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import OutputFormat
from imbue.mng.providers.base_provider import BaseProviderInstance


class LabelCliOptions(CommonCliOptions):
    """Options passed from the CLI to the label command."""

    agents: tuple[str, ...]
    agent_list: tuple[str, ...]
    label: tuple[str, ...]
    label_all: bool
    dry_run: bool


@pure
def parse_label_string(label_str: str) -> tuple[str, str]:
    """Parse a KEY=VALUE label string.

    Raises UserInputError if the format is invalid.
    """
    if "=" not in label_str:
        raise UserInputError(f"Invalid label format: '{label_str}'. Labels must be in KEY=VALUE format.")
    key, value = label_str.split("=", 1)
    if not key:
        raise UserInputError(f"Invalid label format: '{label_str}'. Label key cannot be empty.")
    return key, value


def _read_agent_identifiers_from_stdin() -> list[str]:
    """Read agent identifiers from stdin, one per line.

    Skips empty lines and strips whitespace.
    """
    identifiers: list[str] = []
    for line in sys.stdin:
        stripped = line.strip()
        if stripped:
            identifiers.append(stripped)
    return identifiers


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
            emit_event("label_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if changes:
                write_human_line("Updated labels on {} agent(s)", len(changes))
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _merge_labels(current: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """Merge new labels into current labels, overwriting existing keys."""
    return {**current, **new}


@pure
def _find_matching_agents(
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]],
    agent_identifiers: list[str],
    label_all: bool,
) -> tuple[list[tuple[DiscoveredHost, DiscoveredAgent]], set[str]]:
    """Find agents matching the given identifiers or all agents.

    Returns a tuple of (matched_agents, matched_identifiers).
    """
    matched_agents: list[tuple[DiscoveredHost, DiscoveredAgent]] = []
    matched_identifiers: set[str] = set()

    for host_ref, agent_refs in agents_by_host.items():
        for agent_ref in agent_refs:
            should_include: bool
            if label_all:
                should_include = True
            elif agent_identifiers:
                agent_name_str = str(agent_ref.agent_name)
                agent_id_str = str(agent_ref.agent_id)
                should_include = False
                for identifier in agent_identifiers:
                    if identifier == agent_name_str or identifier == agent_id_str:
                        should_include = True
                        matched_identifiers.add(identifier)
            else:
                should_include = False

            if should_include:
                matched_agents.append((host_ref, agent_ref))

    return matched_agents, matched_identifiers


def _apply_labels_online(
    online_host: OnlineHostInterface,
    agent_ref: DiscoveredAgent,
    labels_to_set: dict[str, str],
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
) -> None:
    """Apply labels to an agent on an online host."""
    for agent in online_host.get_agents():
        if agent.id == agent_ref.agent_id:
            current_labels = agent.get_labels()
            merged_labels = _merge_labels(current_labels, labels_to_set)
            agent.set_labels(merged_labels)
            _output(
                f"Updated labels for agent {agent_ref.agent_name}",
                output_opts,
            )
            changes.append(
                {
                    "agent_id": str(agent_ref.agent_id),
                    "agent_name": str(agent_ref.agent_name),
                    "labels": merged_labels,
                }
            )
            return
    raise AgentNotFoundError(str(agent_ref.agent_id))


def _apply_labels_offline(
    provider: BaseProviderInstance,
    host_ref: DiscoveredHost,
    agent_ref: DiscoveredAgent,
    labels_to_set: dict[str, str],
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
) -> None:
    """Apply labels to an agent on an offline host by updating persisted data.

    Uses the provider's persist_agent_data method to update the agent's
    certified data without requiring the host to be started.
    """
    current_data = dict(agent_ref.certified_data)
    current_labels = dict(current_data.get("labels", {}))
    merged_labels = _merge_labels(current_labels, labels_to_set)
    current_data["labels"] = merged_labels

    provider.persist_agent_data(host_ref.host_id, current_data)

    _output(
        f"Updated labels for agent {agent_ref.agent_name} (offline)",
        output_opts,
    )
    changes.append(
        {
            "agent_id": str(agent_ref.agent_id),
            "agent_name": str(agent_ref.agent_name),
            "labels": merged_labels,
        }
    )


@click.command(name="label")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    multiple=True,
    help="Agent name or ID to label (can be specified multiple times)",
)
@optgroup.option(
    "-a",
    "--all",
    "--all-agents",
    "label_all",
    is_flag=True,
    help="Apply labels to all agents",
)
@optgroup.group("Labels")
@optgroup.option(
    "-l",
    "--label",
    multiple=True,
    help="Label in KEY=VALUE format (repeatable)",
)
@optgroup.group("Behavior")
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be labeled without actually labeling",
)
@add_common_options
@click.pass_context
def label(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="label",
        command_class=LabelCliOptions,
    )
    logger.debug("Started label command")

    # Parse labels
    if not opts.label:
        raise click.UsageError("Must specify at least one label with --label KEY=VALUE")

    labels_to_set: dict[str, str] = {}
    for label_str in opts.label:
        key, value = parse_label_string(label_str)
        labels_to_set[key] = value

    # Collect agent identifiers from args, --agent, and stdin
    agent_identifiers = list(opts.agents) + list(opts.agent_list)

    # Read from stdin if no agent identifiers provided and stdin is not a TTY
    if not agent_identifiers and not opts.label_all:
        try:
            if not sys.stdin.isatty():
                stdin_identifiers = _read_agent_identifiers_from_stdin()
                agent_identifiers.extend(stdin_identifiers)
        except (ValueError, AttributeError):
            pass

    if not agent_identifiers and not opts.label_all:
        raise click.UsageError("Must specify at least one agent, use --all, or pipe agent names via stdin")

    if agent_identifiers and opts.label_all:
        raise click.UsageError("Cannot specify both agent names and --all")

    # Discover all agents
    agents_by_host, _ = discover_all_hosts_and_agents(mng_ctx, include_destroyed=False)

    # Find matching agents
    matched_agents, matched_identifiers = _find_matching_agents(agents_by_host, agent_identifiers, opts.label_all)

    # Verify all specified identifiers were found
    if agent_identifiers:
        unmatched = set(agent_identifiers) - matched_identifiers
        if unmatched:
            unmatched_list = ", ".join(sorted(unmatched))
            raise AgentNotFoundError(f"No agent(s) found matching: {unmatched_list}")

    if not matched_agents:
        _output("No agents found to label", output_opts)
        return

    # Handle dry-run mode
    if opts.dry_run:
        _output("Would apply labels:", output_opts)
        for key, value in labels_to_set.items():
            _output(f"  {key}={value}", output_opts)
        _output("To agents:", output_opts)
        for host_ref, agent_ref in matched_agents:
            _output(f"  - {agent_ref.agent_name} (on host {host_ref.host_id})", output_opts)
        return

    # Apply labels
    changes: list[dict[str, Any]] = []
    for host_ref, agent_ref in matched_agents:
        provider = get_provider_instance(host_ref.provider_name, mng_ctx)
        host = provider.get_host(host_ref.host_id)

        match host:
            case OnlineHostInterface() as online_host:
                _apply_labels_online(
                    online_host=online_host,
                    agent_ref=agent_ref,
                    labels_to_set=labels_to_set,
                    output_opts=output_opts,
                    changes=changes,
                )
            case HostInterface():
                _apply_labels_offline(
                    provider=provider,
                    host_ref=host_ref,
                    agent_ref=agent_ref,
                    labels_to_set=labels_to_set,
                    output_opts=output_opts,
                    changes=changes,
                )
            case _ as unreachable:
                assert_never(unreachable)

    _output_result(changes, output_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="label",
    one_line_description="Set labels on agents",
    synopsis="mng label [AGENTS...] [--agent <AGENT>] [--all] -l KEY=VALUE [-l KEY=VALUE ...]",
    arguments_description="- `AGENTS`: Agent name(s) or ID(s) to label. Can also be read from stdin (one per line) when not provided as arguments.",
    description="""Labels are key-value pairs attached to agents. They are stored in the
agent's certified data and persist across restarts.

Labels are merged with existing labels: new keys are added and existing
keys are updated. To see current labels, use 'mng list'.

Works with both online and offline agents. For offline hosts, labels
are updated directly in the provider's persisted data without requiring
the host to be started.""",
    examples=(
        ("Set a label on an agent", "mng label my-agent --label archived_at=2026-03-15"),
        ("Set multiple labels on multiple agents", "mng label agent1 agent2 -l env=prod -l team=backend"),
        ("Label all agents", "mng label --all --label project=myproject"),
        ("Read agent names from stdin", "mng list --format '{name}' | mng label -l reviewed=true"),
        ("Preview changes", "mng label my-agent --label status=done --dry-run"),
    ),
    see_also=(
        ("list", "List agents and their labels"),
        ("create", "Create an agent with labels"),
    ),
).register()

# Add pager-enabled help option to the label command
add_pager_help_option(label)
