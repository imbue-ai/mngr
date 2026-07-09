"""``mngr donate`` -- spend spare Claude capacity on a donation skill.

Sibling to ``mngr usage`` in this plugin: it reads the same account-level usage
snapshot and, when there's capacity likely to go unused, launches a
non-interactive agent that runs a donation skill (by default the
``document-review`` skill). The capacity test is a Python port of the
``spare-capacity.sh`` recipe (see ``mngr help usage_cron_recipes``): spare when
the 5h window still has budget *and* weekly usage is under the pace line.

The command does a single check-and-maybe-launch tick; it does not schedule
itself. Wire it to cron / a LaunchAgent (or the ``scripts/`` recipes) to donate
idle quota automatically.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

import click
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import OperatorResultPart
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import emit_operator_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import MngrError
from imbue.mngr_usage.api import derive_elapsed
from imbue.mngr_usage.api import gather_usage_snapshots
from imbue.mngr_usage.data_types import UsagePluginConfig
from imbue.mngr_usage.data_types import UsageSnapshot

# The Claude usage writer's source name and its fixed-window keys. These match
# what ``spare-capacity.sh`` selects (`.source == "claude"`, `.five_hour`,
# `.seven_day`) and what the Claude usage writer emits.
CLAUDE_SOURCE = "claude"
FIVE_HOUR_WINDOW = "five_hour"
SEVEN_DAY_WINDOW = "seven_day"

DEFAULT_SKILL = "document-review"
DEFAULT_AGENT_NAME = "donate-extra-quota-bio"

# Spare-capacity thresholds, mirroring spare-capacity.sh exactly:
#   spare  <=>  five_hour.used% < 80  AND  weekly.used% < pace_line(weekly.elapsed%)
# where pace_line starts ~30% under the plain used%==elapsed% line early in the
# 7-day cycle and tapers up to meet it by the cycle's end.
FIVE_HOUR_USED_CEILING = 80.0
WEEKLY_EARLY_MARGIN = 0.30

# Missing readings default to "fully used" so a blank/no-data tick never looks
# like spare capacity -- the same defaults the jq recipe uses (`// 100`).
_ASSUME_USED_WHEN_UNKNOWN = 100.0


class DonateCapacity(FrozenModel):
    """The spare-capacity decision plus the numbers it was made from.

    Carrying the inputs (not just the boolean) lets the command explain itself
    in both human and JSON output, and lets tests assert on the derived values.
    """

    has_spare: bool = Field(description="Whether there is capacity to spend on a donation agent this tick.")
    five_hour_used_percentage: float
    weekly_used_percentage: float
    weekly_elapsed_percentage: float
    weekly_pace_line: float


@pure
def weekly_pace_line(weekly_elapsed_percentage: float) -> float:
    """The weekly used-% ceiling for "under pace", given how far into the cycle we are.

    Ports ``$elw * (1 - 0.30 * (100 - $elw) / 100)``: a line ~30% under the plain
    ``used% == elapsed%`` pace early in the cycle (when elapsed% is small the
    margin is large) that tapers up to meet it as the cycle ends (elapsed% -> 100).
    """
    return weekly_elapsed_percentage * (1 - WEEKLY_EARLY_MARGIN * (100 - weekly_elapsed_percentage) / 100)


@pure
def evaluate_capacity(snapshot: UsageSnapshot | None, now: int) -> DonateCapacity:
    """Decide whether there is spare capacity, from the Claude usage snapshot.

    Mirrors ``spare-capacity.sh``: spare when the 5h window is under
    ``FIVE_HOUR_USED_CEILING`` used *and* weekly usage is under
    :func:`weekly_pace_line`. Absent windows/fields are treated as fully used
    (conservative), so ``None``/blank data yields ``has_spare=False``.
    """
    five_hour = snapshot.windows.get(FIVE_HOUR_WINDOW) if snapshot is not None else None
    seven_day = snapshot.windows.get(SEVEN_DAY_WINDOW) if snapshot is not None else None

    five_hour_used = (
        five_hour.used_percentage
        if five_hour is not None and five_hour.used_percentage is not None
        else _ASSUME_USED_WHEN_UNKNOWN
    )
    weekly_used = (
        seven_day.used_percentage
        if seven_day is not None and seven_day.used_percentage is not None
        else _ASSUME_USED_WHEN_UNKNOWN
    )
    weekly_elapsed = 0.0
    if seven_day is not None:
        _, elapsed_percentage = derive_elapsed(seven_day, now)
        if elapsed_percentage is not None:
            weekly_elapsed = elapsed_percentage

    pace = weekly_pace_line(weekly_elapsed)
    has_spare = five_hour_used < FIVE_HOUR_USED_CEILING and weekly_used < pace
    return DonateCapacity(
        has_spare=has_spare,
        five_hour_used_percentage=five_hour_used,
        weekly_used_percentage=weekly_used,
        weekly_elapsed_percentage=weekly_elapsed,
        weekly_pace_line=pace,
    )


# The donation agent runs headless (``claude --print``) on purpose: a plain
# interactive ``claude`` agent blocks on the first tool-permission prompt, which
# hangs ``mngr create`` and spends none of the quota. A headless agent streams
# and auto-destroys after one pass (so repeat ticks never collide on the name),
# and ``--dangerously-skip-permissions`` lets it actually run the skill's
# commands -- in ``--print`` mode gated tools are otherwise denied, not prompted.
DONATE_AGENT_TYPE = "headless_claude"
DONATE_AGENT_ARGS = ("--dangerously-skip-permissions",)


@pure
def build_create_argv(agent_name: str, skill: str) -> tuple[str, ...]:
    """The ``mngr create`` invocation that launches a donation agent.

    Launches a **headless** claude agent so the donation runs unattended (see
    :data:`DONATE_AGENT_TYPE`). ``--foreground`` is required for headless types
    (it streams output and auto-destroys when done). The skill name is passed as
    the agent's first message; ``--dangerously-skip-permissions`` is spliced in
    after ``--`` so it reaches ``claude`` as an agent arg. Runs from the caller's
    cwd, so invoke ``mngr donate`` from a trusted repo (like the recipes' ``cd``).
    """
    return (
        "mngr",
        "create",
        agent_name,
        DONATE_AGENT_TYPE,
        "--foreground",
        "--message",
        f"Use the {skill} skill",
        "--",
        *DONATE_AGENT_ARGS,
    )


class DonateCliOptions(CommonCliOptions):
    """Options for ``mngr donate`` (plus the common output/logging options)."""

    skill: str
    agent_name: str
    dry_run: bool


def _result_data(capacity: DonateCapacity, opts: DonateCliOptions) -> dict[str, Any]:
    """Structured fields shared by every output branch (JSON + human)."""
    return {
        "has_spare": capacity.has_spare,
        "five_hour_used_percentage": round(capacity.five_hour_used_percentage, 1),
        "weekly_used_percentage": round(capacity.weekly_used_percentage, 1),
        "weekly_elapsed_percentage": round(capacity.weekly_elapsed_percentage, 1),
        "weekly_pace_line": round(capacity.weekly_pace_line, 1),
        "skill": opts.skill,
        "agent_name": opts.agent_name,
    }


@click.command(name="donate")
@click.option(
    "--skill",
    default=DEFAULT_SKILL,
    show_default=True,
    help="Skill the donation agent should run (passed as its first message).",
)
@click.option(
    "--agent-name",
    default=DEFAULT_AGENT_NAME,
    show_default=True,
    help="Name for the created donation agent.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report the spare-capacity decision without creating an agent.",
)
@add_common_options
@click.pass_context
def donate(ctx: click.Context, **kwargs: Any) -> None:
    """Spend spare Claude capacity on a donation skill.

    Reads account-level usage (the same snapshot ``mngr usage`` shows): when the
    5h window still has budget and the week is under pace, create a
    non-interactive agent that runs the donation skill; otherwise do nothing.
    One tick per invocation -- schedule it (``mngr help usage_cron_recipes``) to
    donate idle quota automatically. Run it from a trusted git repo, since the
    created agent is sourced from the current directory.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="donate",
        command_class=DonateCliOptions,
    )
    plugin_config = mngr_ctx.get_plugin_config("usage", UsagePluginConfig)
    now = int(time.time())
    snapshots = gather_usage_snapshots(
        mngr_ctx,
        include_filters=(),
        exclude_filters=(),
        provider_names=None,
        since_seconds=plugin_config.since_seconds,
        now=now,
        include_preserved=True,
    )
    claude_snapshot = next((s for s in snapshots if s.source_name == CLAUDE_SOURCE), None)
    capacity = evaluate_capacity(claude_snapshot, now)
    data = _result_data(capacity, opts)

    if not capacity.has_spare:
        emit_operator_result(
            "donate",
            [
                OperatorResultPart.shown(
                    f"No spare capacity right now (5h used {data['five_hour_used_percentage']}%, "
                    f"weekly used {data['weekly_used_percentage']}% vs pace "
                    f"{data['weekly_pace_line']}%); skipping.",
                    action="skipped",
                    **data,
                )
            ],
            output_opts.output_format,
        )
        return

    if opts.dry_run:
        emit_operator_result(
            "donate",
            [
                OperatorResultPart.shown(
                    f"Spare capacity available -- would create '{opts.agent_name}' to run the "
                    f"{opts.skill} skill (dry run).",
                    action="dry_run",
                    **data,
                )
            ],
            output_opts.output_format,
        )
        return

    argv = build_create_argv(opts.agent_name, opts.skill)
    if shutil.which(argv[0]) is None:
        raise MngrError(f"Could not find '{argv[0]}' on PATH to launch the donation agent.")
    emit_info(
        f"Spare capacity available -- launching '{opts.agent_name}' to run the {opts.skill} skill.",
        output_opts.output_format,
    )
    completed = subprocess.run(list(argv), check=False)
    if completed.returncode != 0:
        raise MngrError(f"`{' '.join(argv)}` exited with status {completed.returncode}.")
    emit_operator_result(
        "donate",
        [
            OperatorResultPart.shown(
                f"Created '{opts.agent_name}' to run the {opts.skill} skill.",
                action="created",
                **data,
            )
        ],
        output_opts.output_format,
    )
