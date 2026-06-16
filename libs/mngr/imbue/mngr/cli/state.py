import sys
from collections.abc import Sequence
from typing import assert_never

import click
from click_option_group import optgroup
from pydantic import BaseModel

from imbue.mngr.api.address_parsers import parse_agent_or_host_address
from imbue.mngr.api.agent_state import CombinedState
from imbue.mngr.api.agent_state import get_agent_details
from imbue.mngr.api.agent_state import get_host_details
from imbue.mngr.api.agent_state import poll_combined_state
from imbue.mngr.api.agent_state import resolve_target
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.field_render import get_field_value
from imbue.mngr.cli.field_render import render_format_template_for_model
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import OutputFormat

# Default fields shown in human output, mirroring the most useful columns of `mngr list`.
_AGENT_HUMAN_FIELDS: tuple[str, ...] = (
    "name",
    "id",
    "type",
    "state",
    "command",
    "host.name",
    "host.provider_name",
    "host.state",
    "url",
    "runtime_seconds",
    "idle_seconds",
    "labels",
)
_HOST_HUMAN_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "provider_name",
    "state",
    "image",
    "uptime_seconds",
    "resource.cpu.count",
    "resource.memory_gb",
)


class StateCliOptions(CommonCliOptions):
    """CLI options for the state command."""

    target: str | None
    quick: bool
    fields: str | None


def _read_target_from_stdin() -> str:
    """Read a target identifier from stdin (one line)."""
    if sys.stdin.isatty():
        write_human_line("Waiting for target on stdin...")
    line = sys.stdin.readline().strip()
    if not line:
        raise click.UsageError("No target provided on stdin")
    return line


def _field_label(field: str) -> str:
    """Turn a field path into a column label, matching `mngr list`'s style."""
    return field.upper().replace(".", " ")


def _label_column_width(fields: Sequence[str]) -> int:
    """Width to which ``LABEL`` columns are aligned in the human view."""
    return max((len(_field_label(f)) for f in fields), default=0)


def _emit_vertical(model: BaseModel, fields: Sequence[str]) -> None:
    """Print one ``LABEL  value`` line per field, label-aligned (single-target human view)."""
    width = _label_column_width(fields)
    for field in fields:
        write_human_line("{}  {}", _field_label(field).ljust(width), get_field_value(model, field))


def _emit_model(
    model: BaseModel, default_fields: Sequence[str], fields: list[str] | None, output_opts: OutputOptions
) -> None:
    """Render a single detail model in the requested format.

    ``--format`` template wins; otherwise JSON dumps the full model and human
    prints a vertical field listing (the requested ``fields`` or the defaults).
    """
    if output_opts.format_template is not None:
        write_human_line(render_format_template_for_model(output_opts.format_template, model))
        return
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            write_json_line(model.model_dump(mode="json"))
        case OutputFormat.HUMAN:
            _emit_vertical(model, fields if fields is not None else default_fields)
        case _ as unreachable:
            assert_never(unreachable)


def _output_agent(details: AgentDetails, fields: list[str] | None, output_opts: OutputOptions) -> None:
    _emit_model(details, _AGENT_HUMAN_FIELDS, fields, output_opts)


def _output_host(
    host_details: HostDetails,
    agent_refs: tuple[DiscoveredAgent, ...],
    fields: list[str] | None,
    output_opts: OutputOptions,
) -> None:
    # JSON carries the host plus a lightweight (id, name) entry per agent on it;
    # the template/human paths describe the host.
    if output_opts.output_format in (OutputFormat.JSON, OutputFormat.JSONL) and output_opts.format_template is None:
        write_json_line(
            {
                "host": host_details.model_dump(mode="json"),
                "agents": [{"id": str(ref.agent_id), "name": str(ref.agent_name)} for ref in agent_refs],
            }
        )
        return
    _emit_model(host_details, _HOST_HUMAN_FIELDS, fields, output_opts)
    # In the default human view, also list the agents on the host (names only).
    if output_opts.output_format == OutputFormat.HUMAN and output_opts.format_template is None and fields is None:
        agent_names = ", ".join(str(ref.agent_name) for ref in agent_refs)
        # Align the AGENTS label to the same column width as the host fields above.
        write_human_line("{}  {}", "AGENTS".ljust(_label_column_width(_HOST_HUMAN_FIELDS)), agent_names or "(none)")


def _output_quick(identifier: str, is_agent_target: bool, state: CombinedState, output_format: OutputFormat) -> None:
    agent_value = state.agent_state.value if state.agent_state is not None else None
    host_value = state.host_state.value if state.host_state is not None else None
    match output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            payload: dict[str, object] = {"identifier": identifier, "host_state": host_value}
            if is_agent_target:
                payload["agent_state"] = agent_value
            write_json_line(payload)
        case OutputFormat.HUMAN:
            if is_agent_target:
                write_human_line("agent  {}", agent_value if agent_value is not None else "UNKNOWN")
            write_human_line("host   {}", host_value if host_value is not None else "UNKNOWN")
        case _ as unreachable:
            assert_never(unreachable)


@click.command(name="state")
@click.argument("target", required=False, default=None)
@optgroup.group("State options")
@optgroup.option(
    "--quick",
    is_flag=True,
    help="Report only the lifecycle state (agent + host), skipping the full detail fetch (cheaper).",
)
@optgroup.option(
    "--fields",
    default=None,
    help="Comma-separated fields to show in human output (same field names as `mngr list`).",
)
@add_common_options
@click.pass_context
def state(ctx: click.Context, **kwargs: object) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="state",
        command_class=StateCliOptions,
        is_format_template_supported=True,
    )

    target_identifier = opts.target
    if target_identifier is None:
        target_identifier = _read_target_from_stdin()

    try:
        address = parse_agent_or_host_address(target_identifier)
    except UserInputError as e:
        raise click.BadParameter(str(e)) from e

    fields = [f.strip() for f in opts.fields.split(",") if f.strip()] if opts.fields else None

    if opts.quick:
        if fields is not None:
            raise click.UsageError("--quick cannot be combined with --fields")
        if output_opts.format_template is not None:
            raise click.UsageError("--quick cannot be combined with a --format template")
        resolved = resolve_target(address, mngr_ctx)
        combined_state = poll_combined_state(resolved)
        _output_quick(resolved.identifier, resolved.is_agent_target, combined_state, output_opts.output_format)
        return

    if isinstance(address, AgentAddress):
        _output_agent(get_agent_details(address, mngr_ctx), fields, output_opts)
    else:
        host_details, agent_refs = get_host_details(address, mngr_ctx)
        _output_host(host_details, agent_refs, fields, output_opts)


CommandHelpMetadata(
    key="state",
    one_line_description="Show the current state and details of a single agent or host",
    synopsis="mngr state [TARGET] [--quick] [--fields FIELDS]",
    description="""Show the current state and details of a single agent or host.

Unlike `mngr list`, which enumerates every provider and then filters, `state`
resolves just the one target (querying only its provider) and fetches only it --
so it is cheap even when you have many agents, as long as you know which one you want.

TARGET can be an agent ID (agent-*), host ID (host-*), or an agent/host name.
If TARGET is omitted, it is read from stdin (one line).

For an agent target, the full agent details are shown (the same fields as `mngr list`,
including host information). For a host target, the host details are shown along with
the agents running on it.

Use --quick to report only the lifecycle state (agent + host) without the full detail
fetch -- this skips plugin field generators and is cheaper, useful for scripting.

Output honors --format (human, json, jsonl, or a template like '{name} {state}') and,
in human mode, --fields to choose which fields to display.""",
    examples=(
        ("Show an agent's state and details", "mngr state my-agent"),
        ("Just the lifecycle state (cheap)", "mngr state my-agent --quick"),
        ("As JSON", "mngr state my-agent --format json"),
        ("Only specific fields", "mngr state my-agent --fields state,host.state,idle_seconds"),
        ("With a template", "mngr state my-agent --format '{name} {state}'"),
        ("A host's state and its agents", "mngr state host-abc123"),
        ("Read target from stdin", "echo agent-abc123 | mngr state"),
    ),
    see_also=(
        ("list", "List all agents and their current states"),
        ("wait", "Wait for an agent or host to reach a target state"),
    ),
).register()

add_pager_help_option(state)
