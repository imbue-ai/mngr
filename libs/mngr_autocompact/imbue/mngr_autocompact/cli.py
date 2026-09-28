from collections.abc import Sequence
from concurrent.futures import Future
from typing import Any

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import group_agents_by_host
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
from imbue.mngr.utils.thread_cleanup import mngr_executor
from imbue.mngr_autocompact.manager import compact_stale_agents
from imbue.mngr_autocompact.manager import get_stale_agents


class AutoCompactCheckCliOptions(CommonCliOptions):
    """CLI options for the autocompact check command."""

    targets: tuple[AgentAddress, ...] = ()
    all: bool = False


class AutoCompactRunCliOptions(CommonCliOptions):
    """CLI options for the autocompact run command."""

    targets: tuple[AgentAddress, ...] = ()
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


def _load_running_compaction_agents_on_host(
    matches_on_host: Sequence[AgentMatch],
    mngr_ctx: MngrContext,
) -> list[AgentInterface]:
    """Load the live agent for each match on one host, requiring it to be running and filtering to those that support compaction."""
    host_match = matches_on_host[0]
    provider = get_provider_instance(host_match.provider_name, mngr_ctx)
    host = provider.get_host(host_match.host_id)
    if not isinstance(host, OnlineHostInterface):
        raise UserInputError(f"Host '{host_match.host_name}' is offline")

    live_agent_by_id = {agent.id: agent for agent in host.get_agents()}
    compaction_agents: list[AgentInterface] = []
    for match in matches_on_host:
        live_agent = live_agent_by_id.get(match.agent_id)
        if live_agent is None or not live_agent.is_running():
            raise UserInputError(f"Agent '{match.agent_name}' is not running on host '{match.host_name}'")
        if not isinstance(live_agent, HasCompactionMixin):
            logger.warning(
                "Agent '{}' of type '{}' does not support context compaction; ignoring",
                match.agent_name,
                live_agent.agent_type,
            )
            continue
        compaction_agents.append(live_agent)
    return compaction_agents


def _resolve_target_agents(
    targets: Sequence[AgentAddress],
    mngr_ctx: MngrContext,
) -> list[AgentInterface]:
    """Resolve target addresses to every running compaction-capable agent they match."""
    matches = find_all_agents(addresses=targets, filter_all=False, target_state=None, mngr_ctx=mngr_ctx)

    # Results are read after the executor exits so that a UserInputError propagates as-is
    # instead of being wrapped in a ConcurrencyExceptionGroup.
    futures: list[Future[list[AgentInterface]]] = []
    with mngr_executor(
        parent_cg=mngr_ctx.concurrency_group,
        name="autocompact_resolve_targets",
        max_workers=32,
    ) as executor:
        for matches_on_host in group_agents_by_host(matches).values():
            futures.append(executor.submit(_load_running_compaction_agents_on_host, matches_on_host, mngr_ctx))
    return [agent for future in futures for agent in future.result()]


@autocompact_group.command(name="check")
@click.argument("targets", type=AGENT_ADDRESS, nargs=-1, required=False)
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

    if not opts.targets and not opts.all:
        raise click.UsageError("Specify an agent target or use --all to check all agents")
    if opts.targets and opts.all:
        raise click.UsageError("Cannot specify both an agent target and --all")

    target_agents = _resolve_target_agents(opts.targets, mngr_ctx) if opts.targets else None
    stale_agents = get_stale_agents(mngr_ctx, agents=target_agents)
    output_check_result(stale_agents, output_opts=output_opts)


@autocompact_group.command(name="run")
@click.argument("targets", type=AGENT_ADDRESS, nargs=-1, required=False)
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

    if not opts.targets and not opts.all:
        raise click.UsageError("Specify an agent target or use --all to compact all agents")
    if opts.targets and opts.all:
        raise click.UsageError("Cannot specify both an agent target and --all")

    target_agents = _resolve_target_agents(opts.targets, mngr_ctx) if opts.targets else None
    compacted = compact_stale_agents(mngr_ctx, agents=target_agents)
    output_run_result(compacted, output_opts=output_opts)


CommandHelpMetadata(
    key="autocompact",
    one_line_description="Automatic context compaction commands for conversational agents",
    synopsis="mngr autocompact (check|run) [TARGETS...] [OPTIONS]",
    description="""Manage automatic context compaction for conversational agents that support context compaction (such as Claude Code agents).

Compaction checks evaluate agent staleness based on prompt cache TTL and context token thresholds. When an agent is running, idle, and near or past cache expiration, context compaction is triggered to reduce prompt token usage and turn latency.""",
    examples=(
        ("Check which agents would be compacted", "mngr autocompact check --all"),
        ("Run compaction on all stale agents", "mngr autocompact run --all"),
        ("Check specific agents", "mngr autocompact check agent-1 agent-2"),
        ("Run compaction on specific agents", "mngr autocompact run agent-1 agent-2"),
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
    synopsis="mngr autocompact check [TARGETS...] [--all] [OPTIONS]",
    description="""Evaluate running conversational agents and report which agents are idle past cache TTL and would be compacted.

Either agent target(s) or the --all flag must be provided.""",
    examples=(
        ("Check a specific agent", "mngr autocompact check my-agent"),
        ("Check multiple specific agents", "mngr autocompact check agent-1 agent-2"),
        ("Check all running agents across all online hosts", "mngr autocompact check --all"),
        ("Output check results in JSON format", "mngr autocompact check --all --format json"),
    ),
).register()

CommandHelpMetadata(
    key="autocompact.run",
    one_line_description="Evaluate agent(s) and trigger context compaction if idle past cache TTL",
    synopsis="mngr autocompact run [TARGETS...] [--all] [OPTIONS]",
    description="""Evaluate running conversational agents and trigger context compaction if idle past cache TTL.

Either agent target(s) or the --all flag must be provided.""",
    examples=(
        ("Compact a specific agent if stale", "mngr autocompact run my-agent"),
        ("Compact multiple specific agents if stale", "mngr autocompact run agent-1 agent-2"),
        ("Compact all running agents across all online hosts if stale", "mngr autocompact run --all"),
        ("Output results in JSON format", "mngr autocompact run --all --format json"),
    ),
).register()

add_pager_help_option(autocompact_group)
add_pager_help_option(check)
add_pager_help_option(run)
