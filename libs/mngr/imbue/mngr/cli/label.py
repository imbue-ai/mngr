from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_agents_by_identifiers_or_state
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
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
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.base_provider import BaseProviderInstance


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


def apply_labels_to_agent_online(
    agent_match: AgentMatch,
    online_host: OnlineHostInterface,
    labels_to_set: dict[str, str],
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
) -> None:
    """Apply labels to a single agent on an online host."""
    for agent in online_host.get_agents():
        if agent.id == agent_match.agent_id:
            current_labels = agent.get_labels()
            merged_labels = _merge_labels(current_labels, labels_to_set)
            agent.set_labels(merged_labels)
            _output(f"Updated labels for agent {agent_match.agent_name}", output_opts)
            changes.append(
                {
                    "agent_id": str(agent_match.agent_id),
                    "agent_name": str(agent_match.agent_name),
                    "labels": merged_labels,
                }
            )
            return
    raise AgentNotFoundOnHostError(agent_match.agent_id, agent_match.host_id)


def apply_labels_to_agents_offline(
    provider: BaseProviderInstance,
    host_id: HostId,
    agent_matches: list[AgentMatch],
    labels_to_set: dict[str, str],
    output_opts: OutputOptions,
    changes: list[dict[str, Any]],
) -> None:
    """Apply labels to agents on an offline host by updating persisted data.

    Uses the provider's persisted agent data to read current labels, merge
    new labels, and write back without requiring the host to be started.

    Raises AgentNotFoundOnHostError if any target agent is not found in
    the persisted records.
    """
    persisted_records = provider.list_persisted_agent_data_for_host(host_id)
    target_ids = {str(m.agent_id) for m in agent_matches}
    found_ids: set[str] = set()

    for record in persisted_records:
        agent_id_str = record.get("id", "")
        if agent_id_str not in target_ids:
            continue

        found_ids.add(agent_id_str)
        current_labels = dict(record.get("labels", {}))
        merged_labels = _merge_labels(current_labels, labels_to_set)
        record["labels"] = merged_labels

        provider.persist_agent_data(host_id, record)

        match_name = next(str(m.agent_name) for m in agent_matches if str(m.agent_id) == agent_id_str)
        _output(f"Updated labels for agent {match_name} (offline)", output_opts)
        changes.append(
            {
                "agent_id": agent_id_str,
                "agent_name": match_name,
                "labels": merged_labels,
            }
        )

    # Report any agents that were not found in persisted data
    missing_ids = target_ids - found_ids
    for missing_id in missing_ids:
        match = next(m for m in agent_matches if str(m.agent_id) == missing_id)
        raise AgentNotFoundOnHostError(match.agent_id, match.host_id)


def apply_labels(
    target_agents: list[AgentMatch],
    labels_to_set: dict[str, str],
    mngr_ctx: MngrContext,
    output_opts: OutputOptions,
) -> list[dict[str, Any]]:
    """Apply labels to all target agents, handling both online and offline hosts.

    Groups agents by host to avoid redundant host lookups.
    """
    changes: list[dict[str, Any]] = []
    agents_by_host = group_agents_by_host(target_agents)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)
        provider_name = agent_list[0].provider_name

        provider = get_provider_instance(provider_name, mngr_ctx)
        host = provider.get_host(HostId(host_id_str))

        match host:
            case OnlineHostInterface() as online_host:
                for agent_match in agent_list:
                    apply_labels_to_agent_online(
                        agent_match=agent_match,
                        online_host=online_host,
                        labels_to_set=labels_to_set,
                        output_opts=output_opts,
                        changes=changes,
                    )
            case HostInterface():
                apply_labels_to_agents_offline(
                    provider=provider,
                    host_id=HostId(host_id_str),
                    agent_matches=agent_list,
                    labels_to_set=labels_to_set,
                    output_opts=output_opts,
                    changes=changes,
                )
            case _ as unreachable:
                assert_never(unreachable)

    return changes


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
    mngr_ctx, output_opts, opts = setup_command_context(
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

    # Collect agent identifiers from args and --agent flag
    agent_identifiers = expand_stdin_placeholder(opts.agents) + list(opts.agent_list)

    if not agent_identifiers and not opts.label_all:
        raise click.UsageError("Must specify at least one agent or use --all (use '-' to read from stdin)")

    if agent_identifiers and opts.label_all:
        raise click.UsageError("Cannot specify both agent names and --all")

    # Find matching agents using the shared infrastructure (same as limit, stop, etc.)
    target_agents = find_agents_by_identifiers_or_state(
        agent_identifiers=agent_identifiers,
        filter_all=opts.label_all,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )

    if not target_agents:
        _output("No agents found to label", output_opts)
        return

    # Handle dry-run mode
    if opts.dry_run:
        _output("Would apply labels:", output_opts)
        for key, value in labels_to_set.items():
            _output(f"  {key}={value}", output_opts)
        _output("To agents:", output_opts)
        for match in target_agents:
            _output(f"  - {match.agent_name} (on host {match.host_id})", output_opts)
        return

    # Apply labels (grouped by host for efficiency)
    changes = apply_labels(target_agents, labels_to_set, mngr_ctx, output_opts)

    _output_result(changes, output_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="label",
    one_line_description="Set labels on agents",
    synopsis="mngr label [AGENTS...|-] [--agent <AGENT>] [--all] -l KEY=VALUE [-l KEY=VALUE ...]",
    arguments_description="- `AGENTS`: Agent name(s) or ID(s) to label. Use '-' to read from stdin (one per line).",
    description="""Labels are key-value pairs attached to agents. They are stored in the
agent's certified data and persist across restarts.

Labels are merged with existing labels: new keys are added and existing
keys are updated. To see current labels, use 'mngr list'.

Works with both online and offline agents. For offline hosts, labels
are updated directly in the provider's persisted data without requiring
the host to be started.""",
    examples=(
        ("Set a label on an agent", "mngr label my-agent --label archived_at=2026-03-15"),
        ("Set multiple labels on multiple agents", "mngr label agent1 agent2 -l env=prod -l team=backend"),
        ("Label all agents", "mngr label --all --label project=myproject"),
        ("Read agent names from stdin", "mngr list --format '{name}' | mngr label - -l reviewed=true"),
        ("Preview changes", "mngr label my-agent --label status=done --dry-run"),
    ),
    see_also=(
        ("list", "List agents and their labels"),
        ("create", "Create an agent with labels"),
    ),
).register()

# Add pager-enabled help option to the label command
add_pager_help_option(label)
