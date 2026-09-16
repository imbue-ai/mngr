from typing import Any

import click
from click_option_group import optgroup

from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode
from imbue.mngr_autocompact.manager import compact_agent_if_stale
from imbue.mngr_autocompact.manager import compact_all_agents
from imbue.mngr_autocompact.manager import get_stale_agents
from imbue.mngr_autocompact.manager import is_agent_stale_for_compaction


class AutoCompactCheckCliOptions(CommonCliOptions):
    """CLI options for the autocompact check command."""

    target: AgentAddress | None = None
    all: bool = False


class AutoCompactRunCliOptions(CommonCliOptions):
    """CLI options for the autocompact run command."""

    target: AgentAddress | None = None
    all: bool = False


@click.group(name="autocompact")
@click.pass_context
def autocompact_group(ctx: click.Context, **kwargs: Any) -> None:
    """Automatic context compaction commands for conversational agents."""


def output_check_result(
    agents: list[AgentName],
    output_opts: OutputOptions,
) -> None:
    """Output the results of a compaction check based on OutputOptions."""
    if output_opts.is_quiet:
        return

    if output_opts.output_format == OutputFormat.JSON:
        write_json_line(
            {
                "agents": [str(name) for name in agents],
            }
        )
    else:
        if agents:
            write_human_line(f"Would compact {len(agents)} agent(s): {', '.join(str(n) for n in agents)}")
        else:
            write_human_line("No agents require compaction.")


def output_run_result(
    compacted: list[AgentName],
    output_opts: OutputOptions,
) -> None:
    """Output the results of a compaction run based on OutputOptions."""
    if output_opts.is_quiet:
        return

    if output_opts.output_format == OutputFormat.JSON:
        write_json_line(
            {
                "compacted": [str(name) for name in compacted],
            }
        )
    else:
        if compacted:
            write_human_line(f"Compacted {len(compacted)} agent(s): {', '.join(str(n) for n in compacted)}")
        else:
            write_human_line("No agents require compaction.")


def _resolve_target_agent(target: AgentAddress, mngr_ctx: MngrContext) -> AgentInterface:
    """Resolve and validate that a target agent exists, is running, and supports compaction."""
    host_ref, agent_ref = find_one_agent(target, mngr_ctx)
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    host = provider.get_host(host_ref.host_id)
    if not isinstance(host, OnlineHostInterface):
        raise UserInputError(f"Host '{host_ref.host_name}' is offline")

    live_agents = {a.id: a for a in host.get_agents()}
    live_agent = live_agents.get(agent_ref.agent_id)
    if live_agent is None or not live_agent.is_running():
        raise UserInputError(f"Agent '{agent_ref.agent_name}' is not running on host '{host_ref.host_name}'")

    if not isinstance(live_agent, HasCompactionMixin):
        raise UserInputError(
            f"Agent '{agent_ref.agent_name}' of type '{live_agent.agent_type}' does not support context compaction"
        )

    return live_agent


@autocompact_group.command(name="check")
@click.argument("target", type=AGENT_ADDRESS, required=False, default=None)
@optgroup.group("Check options")
@optgroup.option(
    "--all",
    "all",
    is_flag=True,
    default=False,
    help="Check all running agents across all hosts.",
)
@add_common_options
@click.pass_context
def check(ctx: click.Context, **kwargs: object) -> None:
    """Check agent(s) and report which would be compacted if idle past cache TTL."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="autocompact.check",
        command_class=AutoCompactCheckCliOptions,
    )

    if opts.target is None and not opts.all:
        raise click.UsageError("Specify an agent target or use --all to check all agents")
    if opts.target is not None and opts.all:
        raise click.UsageError("Cannot specify both an agent target and --all")

    if opts.target is not None:
        live_agent = _resolve_target_agent(opts.target, mngr_ctx)
        config = live_agent.mngr_ctx.get_plugin_config("autocompact", AutoCompactPluginConfig)
        stale_agents = (
            [live_agent.name]
            if is_agent_stale_for_compaction(live_agent, config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER)
            else []
        )
    else:
        stale_agents = get_stale_agents(mngr_ctx)

    output_check_result(stale_agents, output_opts=output_opts)


@autocompact_group.command(name="run")
@click.argument("target", type=AGENT_ADDRESS, required=False, default=None)
@optgroup.group("Run options")
@optgroup.option(
    "--all",
    "all",
    is_flag=True,
    default=False,
    help="Run compaction on all eligible running agents across all hosts.",
)
@add_common_options
@click.pass_context
def run(ctx: click.Context, **kwargs: object) -> None:
    """Evaluate agent(s) and trigger context compaction if idle past cache TTL."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="autocompact.run",
        command_class=AutoCompactRunCliOptions,
    )

    if opts.target is None and not opts.all:
        raise click.UsageError("Specify an agent target or use --all to compact all agents")
    if opts.target is not None and opts.all:
        raise click.UsageError("Cannot specify both an agent target and --all")

    if opts.target is not None:
        live_agent = _resolve_target_agent(opts.target, mngr_ctx)
        config = live_agent.mngr_ctx.get_plugin_config("autocompact", AutoCompactPluginConfig)
        compacted = (
            [live_agent.name]
            if compact_agent_if_stale(live_agent, config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER)
            else []
        )
    else:
        compacted = compact_all_agents(mngr_ctx)

    output_run_result(compacted, output_opts=output_opts)


CommandHelpMetadata(
    key="autocompact",
    one_line_description="Automatic context compaction commands for conversational agents",
    synopsis="mngr autocompact (check|run) [TARGET] [OPTIONS]",
    description="""Manage automatic context compaction for conversational agents that support context compaction (such as Claude Code agents).

Compaction checks evaluate agent staleness based on prompt cache TTL and context token thresholds. When an agent is running, idle, and near or past cache expiration, context compaction is triggered to reduce prompt token usage and turn latency.""",
    examples=(
        ("Check which agents would be compacted", "mngr autocompact check --all"),
        ("Run compaction on all stale agents", "mngr autocompact run --all"),
        ("Check a specific agent", "mngr autocompact check my-agent"),
        ("Run compaction on a specific agent", "mngr autocompact run my-agent"),
        ("Output results in JSON format", "mngr autocompact run --all --format json"),
    ),
    see_also=(
        ("message", "Send a message or prompt to an agent"),
        ("config", "View or set autocompact configuration options"),
    ),
).register()

CommandHelpMetadata(
    key="autocompact.check",
    one_line_description="Check agent(s) and report which would be compacted if idle past cache TTL",
    synopsis="mngr autocompact check [TARGET] [--all] [OPTIONS]",
    description="""Evaluate running conversational agents and report which agents are idle past cache TTL and would be compacted.

Either a specific agent target or the --all flag must be provided.""",
    examples=(
        ("Check a specific agent", "mngr autocompact check my-agent"),
        ("Check all running agents across all online hosts", "mngr autocompact check --all"),
        ("Output check results in JSON format", "mngr autocompact check --all --format json"),
    ),
).register()

CommandHelpMetadata(
    key="autocompact.run",
    one_line_description="Evaluate agent(s) and trigger context compaction if idle past cache TTL",
    synopsis="mngr autocompact run [TARGET] [--all] [OPTIONS]",
    description="""Evaluate running conversational agents and trigger context compaction if idle past cache TTL.

Either a specific agent target or the --all flag must be provided.""",
    examples=(
        ("Compact a specific agent if stale", "mngr autocompact run my-agent"),
        ("Compact all running agents across all online hosts if stale", "mngr autocompact run --all"),
        ("Output results in JSON format", "mngr autocompact run --all --format json"),
    ),
).register()

add_pager_help_option(autocompact_group)
add_pager_help_option(check)
add_pager_help_option(run)
