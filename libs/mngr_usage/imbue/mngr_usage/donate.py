"""``mngr donate`` -- spend spare Claude capacity on a donation skill.

Sibling to ``mngr usage`` in this plugin. One invocation is a single
check-and-maybe-launch *tick*:

1. Read the account-level usage snapshot (the same one ``mngr usage`` shows).
2. Decide whether there's *spare capacity* -- a Python port of the
   ``spare-capacity.sh`` recipe (``mngr help usage_cron_recipes``): spare when
   the rolling 5h window still has budget *and* the 7d window is under a
   tapering pace line (:func:`evaluate_capacity`). Missing readings count as
   "fully used", so a no-data tick never looks spare -- and that case is flagged
   so the CLI can say "can't tell" rather than "maxed out".
3. If spare, launch a *headless* Claude agent (:data:`DONATE_AGENT_TYPE`) that
   runs the donation skill (default ``document-review``) unattended and
   auto-destroys when done; otherwise do nothing. The agent's stream is tee'd to
   a durable per-run log under ``<host_dir>/donate-logs/``.

A single tick spends at most one skill run's worth of quota. To drain spare
capacity over time, ``mngr donate --start`` installs a crontab entry that
re-runs the tick on an interval (``--stop`` removes it) -- the schedule, not any
one tick, is what actually uses up the idle quota.

The launch mechanics (why headless, why the specific ``claude`` flags, why we
pre-clear stale agents/worktrees) are documented at each helper below.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
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
from imbue.mngr.config.host_dir import read_default_host_dir
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

# `mngr donate --start` writes a single crontab entry that re-runs `mngr donate`
# on this interval; each firing re-checks spare capacity and does another batch,
# so the schedule -- not any one tick -- is what drains all the spare quota.
DEFAULT_INTERVAL_MINUTES = 10
# The managed crontab entry is wrapped in these marker lines so --stop can remove
# exactly what --start added, leaving any hand-written crontab lines untouched.
_CRON_BEGIN = "# >>> mngr donate (managed -- edit via `mngr donate --start/--stop`) >>>"
_CRON_END = "# <<< mngr donate (managed) <<<"

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
    has_usage_data: bool = Field(
        description="Whether any Claude usage reading was found. When False the percentages below are the "
        "conservative 'assume fully used' defaults, not real measurements -- so 'no spare' means 'can't "
        "tell', not 'maxed out'."
    )
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
    (conservative), so ``None``/blank data yields ``has_spare=False`` -- but
    ``has_usage_data`` records whether that came from a real reading or the
    default, so the caller can say "can't tell" instead of "maxed out".
    """
    five_hour = snapshot.windows.get(FIVE_HOUR_WINDOW) if snapshot is not None else None
    seven_day = snapshot.windows.get(SEVEN_DAY_WINDOW) if snapshot is not None else None

    five_hour_has_reading = five_hour is not None and five_hour.used_percentage is not None
    weekly_has_reading = seven_day is not None and seven_day.used_percentage is not None

    five_hour_used = five_hour.used_percentage if five_hour_has_reading else _ASSUME_USED_WHEN_UNKNOWN
    weekly_used = seven_day.used_percentage if weekly_has_reading else _ASSUME_USED_WHEN_UNKNOWN
    weekly_elapsed = 0.0
    if seven_day is not None:
        _, elapsed_percentage = derive_elapsed(seven_day, now)
        if elapsed_percentage is not None:
            weekly_elapsed = elapsed_percentage

    pace = weekly_pace_line(weekly_elapsed)
    has_spare = five_hour_used < FIVE_HOUR_USED_CEILING and weekly_used < pace
    return DonateCapacity(
        has_spare=has_spare,
        has_usage_data=five_hour_has_reading or weekly_has_reading,
        five_hour_used_percentage=five_hour_used,
        weekly_used_percentage=weekly_used,
        weekly_elapsed_percentage=weekly_elapsed,
        weekly_pace_line=pace,
    )


# The donation agent runs headless (``claude --print``) on purpose: a plain
# interactive ``claude`` agent blocks on the first tool-permission prompt, which
# hangs ``mngr create`` and spends none of the quota. A headless agent streams
# and auto-destroys after a successful pass, so a completed tick leaves nothing
# behind (a tick that dies mid-way can, which is what the pre-launch cleanup in
# `donate` handles). ``--dangerously-skip-permissions`` (below) is what lets it
# actually run the skill's commands unattended.
DONATE_AGENT_TYPE = "headless_claude"
# ``--output-format stream-json`` (which Claude requires ``--verbose`` to pair
# with ``--print``) is not optional here: mngr's headless runner reads the
# agent's stdout as stream-json, so a bare ``--print`` blob is seen as "no
# output" and the run is reported as failed even when the skill completed. It
# also turns the run into a per-event log -- every tool call and the skill's
# outbound HTTP -- which ``--foreground`` streams live and we tee to a file.
# ``--dangerously-skip-permissions`` lets the unattended agent run the skill's
# commands (in ``--print`` mode gated tools are denied, not prompted).
DONATE_AGENT_ARGS = (
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--dangerously-skip-permissions",
)


@pure
def build_donation_message(skill: str) -> str:
    """The first message the donation agent receives.

    Kept minimal: run the skill and work through whatever it leases, then stop.
    How much is leased per run is the skill's own concern (e.g. document-review's
    ``skill_config.yaml`` ``count``), not something donate can cap from here.
    """
    return f"Use the {skill} skill to complete the work it leases, then stop."


@pure
def build_create_argv(agent_name: str, skill: str, mngr_path: str = "mngr") -> tuple[str, ...]:
    """The ``mngr create`` invocation that launches a donation agent.

    Launches a **headless** claude agent so the donation runs unattended (see
    :data:`DONATE_AGENT_TYPE`). ``--foreground`` is required for headless types
    (it streams output and auto-destroys when done). The skill instruction (see
    :func:`build_donation_message`) is passed as the agent's first message;
    ``--dangerously-skip-permissions`` is spliced in after ``--`` so it reaches
    ``claude`` as an agent arg. Runs from the caller's cwd, so invoke ``mngr
    donate`` from a trusted repo (like the recipes' ``cd``).

    ``mngr_path`` is the mngr executable to spawn. The command passes an absolute
    path (:func:`_current_mngr_path`) so the launch never depends on ``mngr``
    being on ``$PATH`` in whatever environment donate happens to run from.
    """
    return (
        mngr_path,
        "create",
        agent_name,
        DONATE_AGENT_TYPE,
        "--foreground",
        # Source from the repo even with uncommitted changes: donate is meant to
        # run unattended (incl. from cron), and the default clean-tree guard would
        # fail every tick whenever the working repo has edits.
        "--no-ensure-clean",
        "--message",
        build_donation_message(skill),
        "--",
        *DONATE_AGENT_ARGS,
    )


@pure
def build_destroy_argv(agent_name: str, mngr_path: str = "mngr") -> tuple[str, ...]:
    """The ``mngr destroy`` invocation that clears a stale donation agent.

    Run best-effort before :func:`build_create_argv` so a repeat tick never
    collides on the fixed agent name. A headless agent only auto-destroys after
    a *successful* pass, so a launch that failed part-way leaves the name taken;
    ``--reuse`` isn't an option (headless agent types reject it). ``--force``
    skips confirmation and a no-op destroy of a missing agent is harmless.
    ``mngr_path`` is the mngr executable (see :func:`build_create_argv`).
    """
    return (mngr_path, "destroy", agent_name, "--force")


@pure
def build_cron_command(
    mngr_path: str, workdir: str, skill: str, agent_name: str, log_path: str, path_value: str = ""
) -> str:
    """The shell command a scheduled donation tick runs (without the time spec).

    ``cd`` into ``workdir`` first: cron starts in ``$HOME``, but donate must run
    from a trusted git repo (``mngr create`` needs a git root). ``mngr_path`` is
    an absolute path to *this* mngr, since a scheduled ``mngr`` off ``$PATH`` may
    resolve to a different install that lacks ``donate``. ``path_value``, when
    given, is exported first: cron runs with a bare ``/usr/bin:/bin`` PATH that
    omits ``~/.local/bin``, so without it the launched agent can't find ``claude``
    (nor ``git`` from Homebrew) and every tick fails. --start passes the PATH from
    its own environment so scheduled ticks see the same tools the user does. Only
    non-default options are appended; output is appended to ``log_path``.
    """
    command = f"cd {shlex.quote(workdir)} && {shlex.quote(mngr_path)} donate"
    if skill != DEFAULT_SKILL:
        command += f" --skill {shlex.quote(skill)}"
    if agent_name != DEFAULT_AGENT_NAME:
        command += f" --agent-name {shlex.quote(agent_name)}"
    command += f" >> {shlex.quote(log_path)} 2>&1"
    if path_value:
        command = f"export PATH={shlex.quote(path_value)}; {command}"
    return command


@pure
def build_cron_block(interval_minutes: int, command: str) -> str:
    """The full marker-wrapped crontab block for one managed donation schedule."""
    schedule = f"*/{interval_minutes} * * * *"
    return f"{_CRON_BEGIN}\n{schedule} {command}\n{_CRON_END}"


@pure
def remove_managed_cron(existing_crontab: str) -> tuple[str, int]:
    """Strip the managed donation block from a crontab, if present.

    Returns the crontab without any lines between (and including) the marker
    pair, plus the number of lines removed (0 when nothing was managed). Lines
    outside the markers -- the user's own entries -- are preserved verbatim.
    """
    kept: list[str] = []
    removed = 0
    inside_block = False
    for line in existing_crontab.splitlines():
        if line.strip() == _CRON_BEGIN:
            inside_block = True
            removed += 1
            continue
        if line.strip() == _CRON_END:
            inside_block = False
            removed += 1
            continue
        if inside_block:
            removed += 1
            continue
        kept.append(line)
    body = "\n".join(kept).strip("\n")
    return (body + "\n" if body else ""), removed


@pure
def upsert_managed_cron(existing_crontab: str, block: str) -> str:
    """Return ``existing_crontab`` with the managed block replaced (or appended).

    Idempotent: any prior managed block is removed first, so repeated ``--start``
    calls never stack duplicate entries.
    """
    without_managed, _ = remove_managed_cron(existing_crontab)
    if without_managed.strip():
        return without_managed.rstrip("\n") + "\n" + block + "\n"
    return block + "\n"


def _current_mngr_path() -> str:
    """Absolute path to the mngr executable now running, for the crontab entry."""
    candidate = os.path.abspath(sys.argv[0])
    if os.path.isfile(candidate):
        return candidate
    return shutil.which("mngr") or candidate


def _cron_log_path() -> Path:
    """Stable log file the scheduled ticks append to (under the host dir)."""
    return _donate_log_dir() / "cron.log"


def _read_crontab() -> str:
    """The user's current crontab, or empty string when none is installed."""
    result = subprocess.run(("crontab", "-l"), check=False, capture_output=True, text=True)
    # `crontab -l` exits non-zero with "no crontab for user" when none exists.
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(content: str) -> None:
    """Replace the user's crontab with ``content`` (piped to ``crontab -``)."""
    subprocess.run(("crontab", "-"), input=content, text=True, check=True)


def _clear_stale_worktree(agent_name: str) -> None:
    """Best-effort removal of a leftover ``mngr/<agent>`` git worktree + branch.

    mngr creates each agent in a git worktree on a branch named
    ``mngr/<agent-name>``. A run that errors after the worktree is created (or an
    agent that never auto-destroyed) leaves the worktree and branch behind, and
    the next ``mngr create`` fails with "a branch named 'mngr/<agent>' already
    exists" -- ``mngr destroy`` clears the tracked agent but not this orphan.
    Runs ``git`` in the caller's cwd (the repo mngr sourced the agent from). All
    steps are swallowed: a missing worktree/branch is the normal, healthy case.
    """
    branch = f"mngr/{agent_name}"
    listing = subprocess.run(
        ("git", "worktree", "list", "--porcelain"), check=False, capture_output=True, text=True
    )
    worktree_path: str | None = None
    current_path: str | None = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line.strip() == f"branch refs/heads/{branch}":
            worktree_path = current_path
            break
    if worktree_path is not None:
        subprocess.run(
            ("git", "worktree", "remove", "--force", worktree_path), check=False, capture_output=True
        )
    subprocess.run(("git", "worktree", "prune"), check=False, capture_output=True)
    subprocess.run(("git", "branch", "-D", branch), check=False, capture_output=True)


def _donate_log_dir() -> Path:
    """The dir donate writes its run logs to, created if missing.

    Lives under mngr's host dir via :func:`read_default_host_dir` -- the same
    ``MNGR_HOST_DIR``-or-``~/.mngr`` resolution mngr uses everywhere else -- so we
    don't re-derive (and drift from) that path here.
    """
    log_dir = read_default_host_dir() / "donate-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _donation_log_path(agent_name: str, now: int) -> Path:
    """Where to persist one donation agent's streamed event log.

    A headless agent auto-destroys on success, taking its own ``stdout.jsonl``
    with it, and a donation you can't inspect afterwards is one you can't audit --
    so we keep a copy under the host dir. ``now`` (passed in, not read here) keeps
    successive ticks from clobbering each other's logs.
    """
    return _donate_log_dir() / f"{agent_name}-{now}.jsonl"


def _run_and_tee(argv: tuple[str, ...], log_path: Path) -> int:
    """Run ``argv``, streaming its combined output to both stdout and ``log_path``.

    The headless agent emits stream-json (one event per line: tool calls,
    assistant text, the skill's outbound HTTP + submission), so teeing
    line-by-line gives a live view *and* a durable record without holding the
    whole run in memory. Returns the child's exit status.
    """
    with open(log_path, "w", encoding="utf-8") as log_file:
        # Header only in the file (not stdout) so it timestamps the per-run log
        # without double-stamping cron.log, which already gets the tick line above.
        log_file.write(f"===== donate launch {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        log_file.flush()
        process = subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log_file.write(line)
            log_file.flush()
        return process.wait()


class DonateCliOptions(CommonCliOptions):
    """Options for ``mngr donate`` (plus the common output/logging options)."""

    skill: str
    agent_name: str
    dry_run: bool
    start: bool
    stop: bool
    interval_minutes: int


def _result_data(capacity: DonateCapacity, opts: DonateCliOptions) -> dict[str, Any]:
    """Structured fields shared by every output branch (JSON + human)."""
    return {
        "has_spare": capacity.has_spare,
        "has_usage_data": capacity.has_usage_data,
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
@click.option(
    "--start",
    is_flag=True,
    default=False,
    help="Schedule donate to run automatically (installs a crontab entry) and exit.",
)
@click.option(
    "--stop",
    is_flag=True,
    default=False,
    help="Remove the scheduled donate crontab entry and exit.",
)
@click.option(
    "--interval-minutes",
    type=click.IntRange(min=1),
    default=DEFAULT_INTERVAL_MINUTES,
    show_default=True,
    help="With --start: how often the scheduled donate runs.",
)
@add_common_options
@click.pass_context
def donate(ctx: click.Context, **kwargs: Any) -> None:
    """Spend spare Claude capacity on a donation skill.

    Reads account-level usage (the same snapshot ``mngr usage`` shows): when the
    5h window still has budget and the week is under pace, create a
    non-interactive agent that runs the donation skill; otherwise do nothing.
    One tick per invocation -- use ``--start`` to install a crontab entry that
    re-runs it on an interval (``--stop`` removes it), so spare quota is drained
    over many ticks. Run it from a trusted git repo, since the created agent is
    sourced from the current directory.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="donate",
        command_class=DonateCliOptions,
    )

    # --start / --stop manage the crontab schedule and exit; they don't do a tick,
    # so they run before (and independently of) the spare-capacity check.
    if opts.start and opts.stop:
        raise MngrError("Pass only one of --start / --stop.")
    if opts.start:
        command = build_cron_command(
            _current_mngr_path(),
            os.getcwd(),
            opts.skill,
            opts.agent_name,
            str(_cron_log_path()),
            os.environ.get("PATH", ""),
        )
        _write_crontab(upsert_managed_cron(_read_crontab(), build_cron_block(opts.interval_minutes, command)))
        emit_info(
            f"Scheduled donate every {opts.interval_minutes} min from {os.getcwd()} "
            f"(running the {opts.skill} skill; logs -> {_cron_log_path()}).\n"
            f"Stop it with `mngr donate --stop`.",
            output_opts.output_format,
        )
        return
    if opts.stop:
        new_crontab, removed = remove_managed_cron(_read_crontab())
        if removed:
            _write_crontab(new_crontab)
            emit_info("Unscheduled donate (removed the managed crontab entry).", output_opts.output_format)
        else:
            emit_info("No scheduled donate found; nothing to remove.", output_opts.output_format)
        return

    plugin_config = mngr_ctx.get_plugin_config("usage", UsagePluginConfig)
    now = int(time.time())
    # Stamp every tick up front so an accumulating log (esp. cron.log) shows when
    # each run fired and what it decided -- printed for all branches (skip / no
    # data / launch) since it lands before the capacity check.
    emit_info(f"===== donate tick {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))} =====", output_opts.output_format)
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
        if not capacity.has_usage_data:
            since_hours = plugin_config.since_seconds // 3600
            message = (
                f"No Claude usage data in the last {since_hours}h, so spare capacity can't be judged -- "
                f"skipping. (Usage is recorded by mngr-managed Claude agents; run one, e.g. "
                f"`mngr create warmup claude`, to populate it.)"
            )
            action = "no_data"
        else:
            message = (
                f"No spare capacity right now (5h used {data['five_hour_used_percentage']}%, "
                f"weekly used {data['weekly_used_percentage']}% vs pace {data['weekly_pace_line']}%); skipping."
            )
            action = "skipped"
        emit_operator_result(
            "donate",
            [OperatorResultPart.shown(message, action=action, **data)],
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

    mngr_path = _current_mngr_path()
    if shutil.which(mngr_path) is None and not Path(mngr_path).is_file():
        raise MngrError(f"Could not locate an mngr executable to launch the donation agent (tried {mngr_path!r}).")
    argv = build_create_argv(opts.agent_name, opts.skill, mngr_path)
    log_path = _donation_log_path(opts.agent_name, now)
    emit_info(
        f"Spare capacity available -- launching '{opts.agent_name}' to run the {opts.skill} skill.\n"
        f"Streaming its steps below; full event log at {log_path}",
        output_opts.output_format,
    )
    # Clear anything a prior failed tick left behind so the create below can't
    # collide: first the tracked agent, then an orphaned git worktree/branch that
    # `mngr destroy` doesn't cover. Both best-effort -- a missing agent/worktree
    # is the normal case, and a cleanup failure shouldn't mask the create's own.
    subprocess.run(list(build_destroy_argv(opts.agent_name, mngr_path)), check=False, capture_output=True)
    _clear_stale_worktree(opts.agent_name)
    returncode = _run_and_tee(argv, log_path)
    if returncode != 0:
        raise MngrError(f"`{' '.join(argv)}` exited with status {returncode} (see {log_path}).")
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
