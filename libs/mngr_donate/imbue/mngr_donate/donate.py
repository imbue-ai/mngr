"""``mngr donate`` -- spend spare Claude capacity on a donation skill.

Companion to ``mngr usage`` (a separate plugin this one depends on for the
spare-capacity snapshot). One invocation is a single check-and-maybe-launch *tick*:

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
capacity over time, ``mngr donate --start`` installs a launchd LaunchAgent
(macOS) that re-runs the tick on an interval (``--stop`` removes it) -- the
schedule, not any one tick, is what actually uses up the idle quota. launchd
(not cron) because it runs in the login session, the only context that can reach
the keychain where Claude's subscription token lives.

The launch mechanics (why headless, why the specific ``claude`` flags, why we
pre-clear stale agents/worktrees) are documented at each helper below.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

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

# The donation skill (code + prompts) lives in the lab's upstream git repo -- the
# single source of truth. donate checks it out at DEFAULT_SKILL_REF into a host-dir
# cache and runs it from there, so the lab can revise the skill without an mngr
# release. The ref is a branch to *track* (auto-adopt updates) or a pinned commit
# for a reviewed, reproducible version (imbue bumps the ref to adopt updates).
# Both overridable via --skill-repo / --skill-ref.
DEFAULT_SKILL_REPO = (
    "https://gitlab.com/sinnott-armstrong-lab/elsi-checklist/credits-for-science/document-review-skill.git"
)
# Pinned to a reviewed commit (NOT "main") so unattended/scheduled runs execute an
# audited version -- the agent runs this code with --dangerously-skip-permissions.
# Bump this SHA in a reviewed PR to adopt the lab's updates; pass `--skill-ref main`
# to track the branch instead.
DEFAULT_SKILL_REF = "c2e9bbe799c20c9da3896c2205991164f10555fd"

# Optional macOS keychain entry holding a long-lived OAuth token from
# `claude setup-token`. Headless agents can't refresh Claude's short-lived (~8h)
# session token -- the desktop app refreshes only its own copy -- so without this,
# scheduled ticks start failing with `401 Invalid authentication credentials` as
# soon as the session token lapses. See README "Authentication".
OAUTH_KEYCHAIN_SERVICE = "mngr-donate-oauth"

# `mngr donate --start` installs a scheduler that re-runs `mngr donate` on this
# interval; each firing re-checks spare capacity and does another batch, so the
# schedule -- not any one tick -- is what drains all the spare quota. macOS uses a
# launchd LaunchAgent (see build_launchd_plist); other platforms use crontab.
DEFAULT_INTERVAL_MINUTES = 10
# The LaunchAgent's label, and thus its plist filename in ~/Library/LaunchAgents.
LAUNCHD_LABEL = "com.imbue.mngr.donate"

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
def build_donation_message(skill_dir: str) -> str:
    """The first message the donation agent receives.

    Points the agent at the assembled skill dir (pinned code + freshly-pulled
    prompts; see :func:`prepare_skill_dir`) and tells it to run the skill from
    there in manual mode, then stop. We pass an explicit path rather than relying
    on Claude's skill auto-discovery because the skill lives in a host-dir cache,
    not the agent's git worktree.

    The no-repo-changes paragraph is a guard against the *host repo's* hooks: the
    agent works from a worktree of whatever repo donate ran in, so that repo's
    stop hooks (e.g. a reviewer's "no stopping without a PR") apply and once
    goaded a donation agent into opening a junk draft PR. The task touches no
    repo files, so the agent can truthfully say so and stop.
    """
    return (
        f"Follow the instructions in {skill_dir}/SKILL.md to review documents: run its client.py "
        f"from {skill_dir} to lease a work item, review it against the active prompt yourself, and "
        f"submit the result. Complete the work it leases, then stop.\n\n"
        f"This task only talks to the skill's coordination server -- it makes no changes to the "
        f"repository you are running in. Do not commit, push, or open pull requests. If a stop hook "
        f"demands a PR or review, state that this session changed no repository files and stop."
    )


@pure
def build_create_argv(agent_name: str, skill_dir: str, mngr_path: str = "mngr") -> tuple[str, ...]:
    """The ``mngr create`` invocation that launches a donation agent.

    Launches a **headless** claude agent so the donation runs unattended (see
    :data:`DONATE_AGENT_TYPE`). ``--foreground`` is required for headless types
    (it streams output and auto-destroys when done). The skill instruction (see
    :func:`build_donation_message`, pointed at ``skill_dir``) is passed as the
    agent's first message; ``--dangerously-skip-permissions`` is spliced in after
    ``--`` so it reaches ``claude`` as an agent arg. Runs from the caller's cwd, so
    invoke ``mngr donate`` from a trusted repo (like the recipes' ``cd``).

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
        # run unattended (incl. from a schedule), and the default clean-tree guard
        # would fail every tick whenever the working repo has edits.
        "--no-ensure-clean",
        # Share the user's real Claude config/keychain instead of an isolated
        # per-agent copy. Isolated copies of a subscription OAuth token go stale
        # (~24h) and 401 -- fatal for an unattended donor whose owner may never open
        # the interactive claude CLI to refresh them. Forced here so donate works
        # out of the box regardless of the user's agent_types.headless_claude config.
        "-S",
        f"agent_types.{DONATE_AGENT_TYPE}.isolate_local_config_dir=false",
        # Forward the long-lived OAuth token (from the keychain stash or the
        # caller's env; see _donation_agent_env) into the agent's environment.
        # `mngr create` sanitizes the agent env and only forwards vars named via
        # --pass-env, so without this the headless claude agent never sees the
        # token and fails "Not logged in" even though donate put it in the
        # subprocess env. A no-op when the var isn't set (resolve_env_vars skips
        # names not in os.environ), so it's safe unconditionally.
        "--pass-env",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "--message",
        build_donation_message(skill_dir),
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


def _current_mngr_path() -> str:
    """Absolute path to the mngr executable now running, for the LaunchAgent."""
    candidate = os.path.abspath(sys.argv[0])
    if os.path.isfile(candidate):
        return candidate
    return shutil.which("mngr") or candidate


def _schedule_log_path() -> Path:
    """Stable log file the scheduled ticks append to (under the host dir)."""
    return _donate_log_dir() / "schedule.log"


@pure
def build_launchd_plist(
    mngr_path: str, workdir: str, skill: str, agent_name: str, log_path: str, path_value: str, interval_seconds: int
) -> str:
    """The LaunchAgent plist that schedules donate on macOS.

    Preferred over cron on macOS for one decisive reason: a LaunchAgent runs
    inside the user's GUI login session, so it can reach the login **keychain**
    where Claude stores the subscription token -- which cron cannot, making every
    cron-launched tick fail "Not logged in". It also catches up after sleep.

    ``ProgramArguments`` runs mngr directly (no shell); ``WorkingDirectory`` gives
    ``mngr create`` its git root, ``EnvironmentVariables.PATH`` lets it find
    ``claude``/``git`` (launchd starts with a minimal PATH), and stdout/stderr are
    captured to ``log_path``. ``StartInterval`` fires every ``interval_seconds``;
    ``RunAtLoad`` is false so installing doesn't kick off a tick immediately.
    """
    args = [mngr_path, "donate"]
    if skill != DEFAULT_SKILL:
        args += ["--skill", skill]
    if agent_name != DEFAULT_AGENT_NAME:
        args += ["--agent-name", agent_name]
    program_args = "\n".join(f"        <string>{escape(a)}</string>" for a in args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{LAUNCHD_LABEL}</string>\n"
        f"    <key>ProgramArguments</key>\n    <array>\n{program_args}\n    </array>\n"
        f"    <key>WorkingDirectory</key>\n    <string>{escape(workdir)}</string>\n"
        "    <key>EnvironmentVariables</key>\n    <dict>\n"
        f"        <key>PATH</key>\n        <string>{escape(path_value)}</string>\n    </dict>\n"
        f"    <key>StartInterval</key>\n    <integer>{interval_seconds}</integer>\n"
        f"    <key>StandardOutPath</key>\n    <string>{escape(log_path)}</string>\n"
        f"    <key>StandardErrorPath</key>\n    <string>{escape(log_path)}</string>\n"
        "    <key>RunAtLoad</key>\n    <false/>\n"
        "</dict>\n</plist>\n"
    )


def _launchd_plist_path() -> Path:
    """Path to the donate LaunchAgent plist, creating ~/Library/LaunchAgents if needed."""
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    return agents_dir / f"{LAUNCHD_LABEL}.plist"


def _install_launchd(plist: str, interval_minutes: int) -> None:
    """Write the plist and (re)load it into the user's launchd GUI domain."""
    plist_path = _launchd_plist_path()
    plist_path.write_text(plist)
    domain = f"gui/{os.getuid()}"
    # Boot out any prior instance (ignored if absent) before bootstrapping the new
    # one. bootout is asynchronous, so the service can still be tearing down when
    # bootstrap runs -- which surfaces as "Bootstrap failed: 5: Input/output
    # error". Retry through that transient window before giving up.
    subprocess.run(("launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"), check=False, capture_output=True)
    last_error = ""
    for _attempt in range(5):
        result = subprocess.run(
            ("launchctl", "bootstrap", domain, str(plist_path)), check=False, capture_output=True, text=True
        )
        if result.returncode == 0:
            return
        last_error = (result.stderr or result.stdout).strip()
        time.sleep(0.5)
    raise MngrError(f"launchctl bootstrap failed: {last_error}")


def _uninstall_launchd() -> bool:
    """Boot out the LaunchAgent and delete its plist. Returns whether it existed."""
    subprocess.run(("launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"), check=False, capture_output=True)
    plist_path = _launchd_plist_path()
    existed = plist_path.exists()
    plist_path.unlink(missing_ok=True)
    return existed


def _require_macos() -> None:
    """Guard ``--start``/``--stop``: scheduling is implemented via launchd (macOS only).

    A launchd LaunchAgent is the only scheduler that reaches the login keychain
    where Claude's subscription token lives -- cron runs outside the login session
    and every tick fails "Not logged in". Rather than ship a scheduler that
    silently can't authenticate, we scope ``--start`` to macOS; elsewhere, run
    ``mngr donate`` from your own scheduler (its env already has keychain access).
    """
    if sys.platform != "darwin":
        raise MngrError(
            "`mngr donate --start`/`--stop` is macOS-only (it installs a launchd LaunchAgent). "
            "On other platforms, schedule `mngr donate` yourself (e.g. a cron entry that runs it)."
        )


def _install_schedule(skill: str, agent_name: str, interval_minutes: int) -> str:
    """Install the launchd LaunchAgent that runs donate on an interval."""
    _require_macos()
    log_path = str(_schedule_log_path())
    plist = build_launchd_plist(
        _current_mngr_path(),
        os.getcwd(),
        skill,
        agent_name,
        log_path,
        os.environ.get("PATH", ""),
        interval_minutes * 60,
    )
    _install_launchd(plist, interval_minutes)
    return (
        f"Scheduled donate via launchd ({LAUNCHD_LABEL}) every {interval_minutes} min from {os.getcwd()}; "
        f"logs -> {log_path}. It runs in your login session, so it can use your keychain login "
        f"(unlike cron) and catches up after sleep."
    )


def _remove_schedule() -> str:
    """Remove the donate LaunchAgent; returns a status message."""
    _require_macos()
    removed = _uninstall_launchd()
    return (
        "Unscheduled donate (removed the launchd agent)."
        if removed
        else "No scheduled donate found; nothing to remove."
    )


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
    listing = subprocess.run(("git", "worktree", "list", "--porcelain"), check=False, capture_output=True, text=True)
    worktree_path: str | None = None
    current_path: str | None = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line.strip() == f"branch refs/heads/{branch}":
            worktree_path = current_path
            break
        else:
            continue
    if worktree_path is not None:
        subprocess.run(("git", "worktree", "remove", "--force", worktree_path), check=False, capture_output=True)
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


def _donate_skill_dir(skill_name: str) -> Path:
    """The host-dir cache where donate checks out the skill repo.

    Under mngr's host dir (like the logs), so it persists across ticks -- the last
    good checkout survives a later network hiccup.
    """
    return read_default_host_dir() / "donate-skills" / skill_name


def prepare_skill_dir(skill_name: str, skill_repo: str, skill_ref: str) -> Path:
    """Check out the donation skill (code + prompts) from its upstream repo.

    The whole skill is owned by the lab's repo (``skill_repo``) -- the single source
    of truth. donate clones it into a host-dir cache and checks out ``skill_ref``: a
    branch to *track* (adopt updates on the next tick) or a pinned commit for a
    reviewed, reproducible version (imbue bumps the ref to adopt updates). The agent
    runs the skill from the returned checkout. A network failure that leaves a usable
    existing checkout reuses it; a first run with no usable checkout is fatal.

    donate never runs this repo's code itself -- it hands the path to the headless
    agent -- but the agent runs with ``--dangerously-skip-permissions``, so pin
    ``skill_ref`` to a reviewed commit for anything unattended/scheduled.
    """
    cache = _donate_skill_dir(skill_name)
    if (cache / ".git").is_dir():
        subprocess.run(
            ("git", "-C", str(cache), "fetch", "--quiet", "--tags", "origin"), check=False, capture_output=True
        )
    else:
        shutil.rmtree(cache, ignore_errors=True)
        subprocess.run(("git", "clone", "--quiet", skill_repo, str(cache)), check=False, capture_output=True)
    if (cache / ".git").is_dir():
        subprocess.run(("git", "-C", str(cache), "checkout", "--quiet", skill_ref), check=False, capture_output=True)
        # If skill_ref is a branch, advance to the fetched upstream tip; a harmless
        # no-op when it's a pinned commit (no origin/<sha> ref exists).
        subprocess.run(
            ("git", "-C", str(cache), "reset", "--hard", "--quiet", f"origin/{skill_ref}"),
            check=False,
            capture_output=True,
        )
    if not (cache / "SKILL.md").is_file():
        raise MngrError(
            f"Could not check out ref '{skill_ref}' of donation skill repo {skill_repo} (no SKILL.md in {cache})."
        )
    return cache


def _donation_log_path(agent_name: str, now: int) -> Path:
    """Where to persist one donation agent's streamed event log.

    A headless agent auto-destroys on success, taking its own ``stdout.jsonl``
    with it, and a donation you can't inspect afterwards is one you can't audit --
    so we keep a copy under the host dir. ``now`` (passed in, not read here) keeps
    successive ticks from clobbering each other's logs.
    """
    return _donate_log_dir() / f"{agent_name}-{now}.jsonl"


@pure
def build_agent_env(base_env: dict[str, str], stashed_token: str | None) -> dict[str, str] | None:
    """The donation agent's environment: ``base_env`` plus the stashed OAuth token.

    Returns None (inherit unchanged) when the variable is already set -- an
    explicit override wins -- or when there's no stashed token to add.
    """
    if base_env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return None
    if stashed_token is None:
        return None
    return {**base_env, "CLAUDE_CODE_OAUTH_TOKEN": stashed_token}


def _read_stashed_oauth_token() -> str | None:
    """The long-lived OAuth token from the macOS keychain, or None if not stashed.

    This is the year-long token from `claude setup-token`, stored under
    OAUTH_KEYCHAIN_SERVICE (see README "Authentication"). Exporting it as
    CLAUDE_CODE_OAUTH_TOKEN lets the headless agent outlive the ~8h session
    token it can't refresh.
    """
    if sys.platform != "darwin":
        return None
    result = subprocess.run(
        ("security", "find-generic-password", "-s", OAUTH_KEYCHAIN_SERVICE, "-w"),
        check=False,
        capture_output=True,
        text=True,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        return None
    return token


def _donation_agent_env() -> dict[str, str] | None:
    """Environment for the donation agent, with the stashed token if one exists."""
    base_env = dict(os.environ)
    if base_env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return None
    return build_agent_env(base_env, _read_stashed_oauth_token())


def _run_and_tee(argv: tuple[str, ...], log_path: Path, env: dict[str, str] | None = None) -> int:
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
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env
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
    skill_repo: str
    skill_ref: str
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
    help="Donation skill to run (the subdir name under the host-dir skill cache).",
)
@click.option(
    "--skill-repo",
    default=DEFAULT_SKILL_REPO,
    show_default=True,
    help="Upstream git repo the skill (code + prompts) is checked out from.",
)
@click.option(
    "--skill-ref",
    default=DEFAULT_SKILL_REF,
    show_default=True,
    help="Git ref to check out: a branch to track, or a pinned commit for a reviewed version.",
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
    help="Schedule donate to run automatically (installs a launchd LaunchAgent; macOS only) and exit.",
)
@click.option(
    "--stop",
    is_flag=True,
    default=False,
    help="Remove the scheduled donate LaunchAgent and exit.",
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
    One tick per invocation -- use ``--start`` to install a launchd LaunchAgent
    (macOS) that re-runs it on an interval (``--stop`` removes it), so spare quota
    is drained over many ticks. Run it from a trusted git repo, since the created
    agent is sourced from the current directory.
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
        message = _install_schedule(opts.skill, opts.agent_name, opts.interval_minutes)
        emit_info(f"{message}\nStop it with `mngr donate --stop`.", output_opts.output_format)
        return
    if opts.stop:
        emit_info(_remove_schedule(), output_opts.output_format)
        return

    plugin_config = mngr_ctx.get_plugin_config("usage", UsagePluginConfig)
    now = int(time.time())
    # Stamp every tick up front so an accumulating log (esp. cron.log) shows when
    # each run fired and what it decided -- printed for all branches (skip / no
    # data / launch) since it lands before the capacity check.
    emit_info(
        f"===== donate tick {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))} =====", output_opts.output_format
    )
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
    # Assemble the skill dir (pinned code + freshly-pulled prompts) before launch,
    # and point the agent at it.
    skill_dir = prepare_skill_dir(opts.skill, opts.skill_repo, opts.skill_ref)
    argv = build_create_argv(opts.agent_name, str(skill_dir), mngr_path)
    log_path = _donation_log_path(opts.agent_name, now)
    emit_info(
        f"Spare capacity available -- launching '{opts.agent_name}' to run the {opts.skill} skill "
        f"from {skill_dir}.\nStreaming its steps below; full event log at {log_path}",
        output_opts.output_format,
    )
    # Clear anything a prior failed tick left behind so the create below can't
    # collide: first the tracked agent, then an orphaned git worktree/branch that
    # `mngr destroy` doesn't cover. Both best-effort -- a missing agent/worktree
    # is the normal case, and a cleanup failure shouldn't mask the create's own.
    subprocess.run(list(build_destroy_argv(opts.agent_name, mngr_path)), check=False, capture_output=True)
    _clear_stale_worktree(opts.agent_name)
    returncode = _run_and_tee(argv, log_path, env=_donation_agent_env())
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
