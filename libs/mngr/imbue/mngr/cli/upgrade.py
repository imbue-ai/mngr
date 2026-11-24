import os
from collections.abc import Iterable
from collections.abc import Sequence
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup

from imbue.imbue_common.pure import pure
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import order_agent_matches_as_named
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.api.upgrade import AgentCurrent
from imbue.mngr.api.upgrade import AgentUpgradeFailed
from imbue.mngr.api.upgrade import AgentUpgradeOutcome
from imbue.mngr.api.upgrade import AgentUpgradeSkipped
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import UpgradeFailedStep
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.api.upgrade import format_agent_label
from imbue.mngr.api.upgrade import upgrade_agents
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import parse_agent_addresses_or_raise
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.exit_codes import EXIT_CODE_AGENTS_NOT_UPGRADED
from imbue.mngr.cli.exit_codes import EXIT_CODE_ERROR
from imbue.mngr.cli.exit_codes import EXIT_CODE_SUCCESS
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.stdin_utils import STDIN_PLACEHOLDER
from imbue.mngr.cli.stdin_utils import expand_stdin_placeholder
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat


class UpgradeCliOptions(CommonCliOptions):
    """Options passed from the CLI to the upgrade command."""

    agents: tuple[str, ...]
    agent_list: tuple[AgentAddress, ...]
    force: bool
    dry_run: bool
    on_error: str


@pure
def format_upgrade_outcome(outcome: AgentUpgradeOutcome) -> str:
    """The agent's line of human output; a dry run lists the paths it would rewrite under the line."""
    agent_label = format_agent_label(outcome.agent_name, outcome.host)
    match outcome:
        case AgentUpgraded():
            return _format_upgraded(outcome, agent_label)
        case AgentCurrent():
            return f"current {agent_label}"
        case AgentUpgradeSkipped():
            verb = "would skip" if outcome.is_dry_run else "skipped"
            return f"{verb} {agent_label}: {_join_lines(outcome.detail)}"
        case AgentUpgradeFailed():
            return (
                f"failed {agent_label}: {outcome.step.lower()} ({_join_lines(outcome.error_message)}); "
                f"{_describe_agent_after_failed_step(outcome.step, agent_label)}"
            )
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _join_lines(text: str) -> str:
    """``text``'s non-blank lines joined by "; ", so a multi-line error stays on its agent's line."""
    return "; ".join(line.strip() for line in text.splitlines() if line.strip())


@pure
def _format_upgraded(outcome: AgentUpgraded, agent_label: str) -> str:
    match outcome.plan:
        case UpgradePlan.LEAVE_STOPPED:
            detail = "stopped"
        case UpgradePlan.RESTART_IDLE:
            detail = "restart" if outcome.is_dry_run else "restarted"
        case UpgradePlan.RESTART_INTERRUPTING if outcome.prior_state == AgentLifecycleState.DONE:
            detail = "restart; its program has exited" if outcome.is_dry_run else "restarted; its program had exited"
        case UpgradePlan.RESTART_INTERRUPTING:
            detail = (
                f"interrupts {outcome.prior_state}"
                if outcome.is_dry_run
                else f"restarted; interrupted {outcome.prior_state}"
            )
        case _ as unreachable:
            assert_never(unreachable)
    if not outcome.is_dry_run:
        return f"upgraded {agent_label} ({detail})"
    return "\n".join([f"would upgrade {agent_label} ({detail})", *(f"  {path}" for path in outcome.changed_paths)])


@pure
def _describe_agent_after_failed_step(step: UpgradeFailedStep, agent_label: str) -> str:
    match step:
        case UpgradeFailedStep.LOCK | UpgradeFailedStep.PREPARE | UpgradeFailedStep.START_CHECK:
            return "untouched"
        case UpgradeFailedStep.STOP:
            return "files unchanged; the agent may be stopped"
        case UpgradeFailedStep.SWAP:
            return "left stopped; some files may already be replaced"
        case UpgradeFailedStep.START:
            return f"not running on the new files; start it with `mngr start {agent_label} --restart`"
        case UpgradeFailedStep.UNLOCK:
            return "its upgrade ran, but another mngr may have changed it while the host lock was not held"
        case UpgradeFailedStep.READY:
            return (
                "started but did not signal readiness; session left for inspection; "
                f"restart it with `mngr start {agent_label} --restart`"
            )
        case _ as unreachable:
            assert_never(unreachable)


@pure
def get_upgrade_exit_code(outcomes: Sequence[AgentUpgradeOutcome]) -> int:
    """``EXIT_CODE_ERROR`` if an agent failed, else ``EXIT_CODE_AGENTS_NOT_UPGRADED`` if one was skipped, else success."""
    if any(isinstance(outcome, AgentUpgradeFailed) for outcome in outcomes):
        return EXIT_CODE_ERROR
    if any(isinstance(outcome, AgentUpgradeSkipped) for outcome in outcomes):
        return EXIT_CODE_AGENTS_NOT_UPGRADED
    return EXIT_CODE_SUCCESS


@pure
def get_upgrade_outcome_event_name(outcome: AgentUpgradeOutcome) -> str:
    match outcome:
        case AgentUpgraded():
            return "agent_upgraded"
        case AgentCurrent():
            return "agent_current"
        case AgentUpgradeSkipped():
            return "agent_skipped"
        case AgentUpgradeFailed():
            return "agent_failed"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def format_upgrade_summary(outcomes: Sequence[AgentUpgradeOutcome], is_dry_run: bool) -> str:
    upgraded_label = "would be upgraded" if is_dry_run else "upgraded"
    skipped_label = "would not be upgraded" if is_dry_run else "not upgraded"
    return (
        f"{_count_outcomes(outcomes, AgentUpgraded)} {upgraded_label}, "
        f"{_count_outcomes(outcomes, AgentCurrent)} current, "
        f"{_count_outcomes(outcomes, AgentUpgradeSkipped)} {skipped_label}, "
        f"{_count_outcomes(outcomes, AgentUpgradeFailed)} failed"
    )


@pure
def build_upgrade_summary_data(outcomes: Sequence[AgentUpgradeOutcome], is_dry_run: bool) -> dict[str, Any]:
    """The fields of the JSONL ``upgrade_result`` event: the count of each kind of outcome and the exit code."""
    return {
        "dry_run": is_dry_run,
        "upgraded_count": _count_outcomes(outcomes, AgentUpgraded),
        "current_count": _count_outcomes(outcomes, AgentCurrent),
        "skipped_count": _count_outcomes(outcomes, AgentUpgradeSkipped),
        "failed_count": _count_outcomes(outcomes, AgentUpgradeFailed),
        "exit_code": get_upgrade_exit_code(outcomes),
    }


@pure
def build_upgrade_result_data(outcomes: Sequence[AgentUpgradeOutcome], is_dry_run: bool) -> dict[str, Any]:
    """The object ``--format json`` prints: every outcome's fields grouped by kind, then the summary fields."""
    return {
        "upgraded_agents": _dump_outcomes(outcomes, AgentUpgraded),
        "current_agents": _dump_outcomes(outcomes, AgentCurrent),
        "skipped_agents": _dump_outcomes(outcomes, AgentUpgradeSkipped),
        "failed_agents": _dump_outcomes(outcomes, AgentUpgradeFailed),
        **build_upgrade_summary_data(outcomes, is_dry_run),
    }


@pure
def _count_outcomes(outcomes: Sequence[AgentUpgradeOutcome], outcome_class: type[AgentUpgradeOutcome]) -> int:
    return sum(isinstance(outcome, outcome_class) for outcome in outcomes)


@pure
def _dump_outcomes(
    outcomes: Sequence[AgentUpgradeOutcome], outcome_class: type[AgentUpgradeOutcome]
) -> list[dict[str, Any]]:
    return [_dump_outcome(outcome) for outcome in outcomes if isinstance(outcome, outcome_class)]


@pure
def _dump_outcome(outcome: AgentUpgradeOutcome) -> dict[str, Any]:
    """The outcome's fields less ``is_dry_run``, which the output reports as ``dry_run``."""
    return outcome.model_dump(mode="json", exclude={"is_dry_run"})


def emit_upgrade_outcomes(
    outcomes: Iterable[AgentUpgradeOutcome], is_dry_run: bool, output_format: OutputFormat
) -> list[AgentUpgradeOutcome]:
    """Write each outcome as it arrives, then the run's result, and return the outcomes.

    When an error escapes the run, JSON output still gets the result gathered so far, with the error exit code.
    """
    emitted_outcomes: list[AgentUpgradeOutcome] = []
    is_run_finished = False
    try:
        for outcome in outcomes:
            _emit_upgrade_outcome(outcome, is_dry_run, output_format)
            emitted_outcomes.append(outcome)
        is_run_finished = True
    finally:
        if not is_run_finished and output_format == OutputFormat.JSON:
            write_json_line({**build_upgrade_result_data(emitted_outcomes, is_dry_run), "exit_code": EXIT_CODE_ERROR})
    _emit_upgrade_result(emitted_outcomes, is_dry_run, output_format)
    return emitted_outcomes


def _emit_upgrade_outcome(outcome: AgentUpgradeOutcome, is_dry_run: bool, output_format: OutputFormat) -> None:
    match output_format:
        case OutputFormat.HUMAN:
            write_human_line(format_upgrade_outcome(outcome))
        case OutputFormat.JSONL:
            emit_event(
                get_upgrade_outcome_event_name(outcome),
                {**_dump_outcome(outcome), "dry_run": is_dry_run},
                OutputFormat.JSONL,
            )
        case OutputFormat.JSON:
            pass
        case _ as unreachable:
            assert_never(unreachable)


def _emit_upgrade_result(
    outcomes: Sequence[AgentUpgradeOutcome], is_dry_run: bool, output_format: OutputFormat
) -> None:
    match output_format:
        case OutputFormat.HUMAN:
            if len(outcomes) > 1:
                write_human_line(format_upgrade_summary(outcomes, is_dry_run))
        case OutputFormat.JSONL:
            emit_event("upgrade_result", build_upgrade_summary_data(outcomes, is_dry_run), OutputFormat.JSONL)
        case OutputFormat.JSON:
            write_json_line(build_upgrade_result_data(outcomes, is_dry_run))
        case _ as unreachable:
            assert_never(unreachable)


@click.command(name="upgrade")
@click.argument("agents", nargs=-1, required=False)
@optgroup.group("Target Selection")
@optgroup.option(
    "--agent",
    "agent_list",
    type=AGENT_ADDRESS,
    multiple=True,
    help="Agent address (NAME[@HOST[.PROVIDER]]) to upgrade (can be specified multiple times)",
)
@optgroup.group("Behavior")
@optgroup.option(
    "-f",
    "--force",
    is_flag=True,
    help="Also upgrade busy agents (mid-turn, on a dialog, running background work, or with another program in their terminal) and agents whose program has exited, restarting them",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be upgraded without changing anything",
)
@optgroup.group("Error Handling")
@optgroup.option(
    "--on-error",
    type=click.Choice(["abort", "continue"], case_sensitive=False),
    default="continue",
    show_default=True,
    help="What to do when errors occur: abort (stop immediately) or continue (keep going)",
)
@add_common_options
@click.pass_context
def upgrade(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="upgrade",
        command_class=UpgradeCliOptions,
    )

    agent_addresses: list[AgentAddress] = parse_agent_addresses_or_raise(expand_stdin_placeholder(opts.agents)) + list(
        opts.agent_list
    )
    if not agent_addresses:
        if STDIN_PLACEHOLDER not in opts.agents:
            raise click.UsageError("Must specify at least one agent (use '-' to read from stdin)")
        return

    matches = order_agent_matches_as_named(
        agent_addresses,
        find_all_agents(addresses=agent_addresses, filter_all=False, target_state=None, mngr_ctx=mngr_ctx),
    )
    caller_agent_id_value = os.environ.get("MNGR_AGENT_ID")
    outcomes = emit_upgrade_outcomes(
        upgrade_agents(
            matches,
            is_forced=opts.force,
            is_dry_run=opts.dry_run,
            error_behavior=ErrorBehavior(opts.on_error.upper()),
            caller=(
                AgentInstanceKey.build(AgentId(caller_agent_id_value), get_local_host(mngr_ctx).id)
                if caller_agent_id_value
                else None
            ),
            mngr_ctx=mngr_ctx,
        ),
        opts.dry_run,
        output_opts.output_format,
    )
    exit_code = get_upgrade_exit_code(outcomes)
    if exit_code != EXIT_CODE_SUCCESS:
        ctx.exit(exit_code)


CommandHelpMetadata(
    key="upgrade",
    one_line_description="Upgrade existing agents to match the version of mngr running this 'mngr upgrade' command",
    synopsis="mngr upgrade [AGENTS...|-] [--agent <AGENT>] [-f|--force] [--dry-run] [--on-error <MODE>]",
    arguments_description="- `AGENTS`: Agent name(s) or ID(s) to upgrade, or `-` to read them from stdin.",
    description="""When mngr creates an agent, it writes scripts and hooks into the agent.
Those files stay at the version of mngr that created the agent, while the
mngr you run later reads what they record. `mngr upgrade` rewrites them
to match the version of mngr running the command, so an agent created by
an older mngr works the way the newer one expects, without being
recreated. Only those files change: the agent keeps its options, branch
and conversation.

An idle agent is restarted (closing any attached terminal) and resumes
its conversation. A stopped agent gets the new files and stays stopped;
one whose host is not running is skipped, and its host is not started.
An agent that is busy, or whose program has exited, is skipped unless
--force.

Each agent is reported as:
- upgraded: its files were rewritten.
- current: nothing to change, so re-running is safe.
- skipped: left as it was; the line says why.
- failed: the line names the step that failed.

Exit codes:
- 0: every agent was upgraded or is current.
- 9: at least one was skipped, and none failed.
- 1: at least one failed.""",
    examples=(
        ("Upgrade one agent", "mngr upgrade my-agent"),
        ("See what it would change, changing nothing", "mngr upgrade my-agent --dry-run"),
        ("Preview upgrading every local agent", "mngr list --local --ids | mngr upgrade - --dry-run"),
        ("Upgrade every local agent", "mngr list --local --ids | mngr upgrade -"),
        ("Upgrade a busy agent, interrupting it", "mngr upgrade my-agent --force"),
    ),
).register()

add_pager_help_option(upgrade)
