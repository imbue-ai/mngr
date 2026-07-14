"""open-seer hourly tick — the only deterministic code in the system.

One Modal cron function (app "open-seer") that, per DESIGN.md §0.2/§2/§3/§9:

  1. exits silently unless OPEN_SEER_ENABLED is truthy (kill switch);
  2. mirrors MIRROR_SOURCE_REPO -> MIRROR_REPO every tick — push main and
     all new/updated branches, NEVER deleting anything on the mirror;
  3. queries Sentry for unassigned + unresolved error/fatal issues across
     SENTRY_PROJECT_PREFIX projects (error category only; regressed issues
     are a human's job and are never queried);
  4. exits silently if there are none;
  5. logs an ERROR and does NOT spawn if a previous sweep-* agent is still
     running (overlap guard — a still-running sweep is a signal something
     is wrong, not a scheduling event);
  6. otherwise spawns the sweep agent on its own host with the prompt and
     env baked in:
       mngr create sweep-<UTC yyyymmddhhmmss> --provider modal --new-host
           --pass-env ... --message "/sentry-sweep <issues>"
     with an idle-timeout so the finished sweep self-stops. The sweep gets
     its own host because this cron container is torn down the moment the
     tick returns — a local agent would die mid-triage.

All judgment (grouping, diagnosis, fixing) lives in the agents and their
skills under .claude/skills/ — this file stays about a page.

Local one-shot tick (for Docker testing):
    python tick.py
Dry run (mirror push printed instead of executed; the sweep is still
spawned, with OPEN_SEER_DRY_RUN forwarded so the sweep itself prints its
intended writes to its transcript instead of executing them — DESIGN §11):
    OPEN_SEER_ENABLED=1 OPEN_SEER_DRY_RUN=1 python tick.py

Deploy: `modal deploy tick.py`. Env comes from a single Modal secret named
"open-seer" carrying the DESIGN.md §9 variables (SENTRY_AUTH_TOKEN,
SENTRY_ORG, SENTRY_PROJECT_PREFIX, GITHUB_TOKEN, MIRROR_SOURCE_REPO,
MIRROR_REPO, ANTHROPIC_API_KEY, OPEN_SEER_ENABLED, OPEN_SEER_DRY_RUN, ...).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from datetime import timezone

import requests

log = logging.getLogger("open-seer")

SENTRY_API = "https://sentry.io/api/0"
# Error-category issues only (DESIGN.md §3). Regressed issues stay with
# humans (§8) — is:regressed must never appear here.
ISSUE_QUERY = "is:unresolved is:unassigned issue.category:error level:[error,fatal]"
# A sweep only dispatches fixers and stops; once idle it is killed (§2).
SWEEP_IDLE_TIMEOUT = "30m"
# Hard ceiling on the sweep sandbox's lifetime (mngr modal build arg). The
# idle-timeout reaps the sweep much sooner; without this ceiling raised, the
# provider's default max sandbox age (15 minutes) kills it mid-triage.
SWEEP_SANDBOX_TIMEOUT_SECONDS = 2 * 60 * 60
# Env forwarded to the sweep's fresh host, which inherits nothing from this
# container (sentry-sweep SKILL.md §1 requires these).
SWEEP_PASS_ENV = (
    "SENTRY_AUTH_TOKEN",
    "SENTRY_ORG",
    "SENTRY_PROJECT_PREFIX",
    "SENTRY_TEAM",
    "GITHUB_TOKEN",
    "TARGET_REPO",
    "ANTHROPIC_API_KEY",
    "OPEN_SEER_MAX_FIXERS",
    "OPEN_SEER_DRY_RUN",
    "OPEN_SEER_ENABLED",
)
# Force-updates existing branches and creates new ones; deletes nothing
# (non-empty source side — a deletion refspec has an empty source).
MIRROR_PUSH_REFSPEC = "+refs/remotes/origin/*:refs/heads/*"
# mngr AgentLifecycleState values that count as "still running" for the
# overlap guard; UNKNOWN fails closed.
RUNNING_STATES = {"RUNNING", "WAITING", "RUNNING_UNKNOWN_AGENT_TYPE", "UNKNOWN"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


# --- Sentry ------------------------------------------------------------


def _sentry_get_paginated(path: str, token: str, params: dict | None, max_pages: int = 20) -> list[dict]:
    """GET a Sentry collection endpoint, following Link-header pagination."""
    results: list[dict] = []
    url = f"{SENTRY_API}{path}"
    for _ in range(max_pages):
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
        resp.raise_for_status()
        results.extend(resp.json())
        nxt = resp.links.get("next", {})
        if nxt.get("results") != "true":
            break
        url, params = nxt["url"], None
    return results


def fetch_projects(org: str, token: str) -> list[dict]:
    return _sentry_get_paginated(f"/organizations/{org}/projects/", token, {"per_page": 100})


def filter_projects(projects: list[dict], prefix: str) -> list[dict]:
    return [p for p in projects if str(p.get("slug", "")).startswith(prefix)]


def fetch_issues(org: str, projects: list[dict], token: str) -> list[dict]:
    issues: list[dict] = []
    for project in projects:
        issues.extend(
            _sentry_get_paginated(
                f"/organizations/{org}/issues/",
                token,
                {"query": ISSUE_QUERY, "project": project["id"], "limit": 100},
            )
        )
    return issues


# --- Sweep dispatch ----------------------------------------------------


def compact_issue(issue: dict) -> dict:
    """The compact per-issue payload the sweep agent starts from."""
    return {
        "shortId": issue.get("shortId"),
        "id": issue.get("id"),
        "project": (issue.get("project") or {}).get("slug"),
        "title": issue.get("title"),
        "culprit": issue.get("culprit"),
        "level": issue.get("level"),
        "count": issue.get("count"),
        "firstSeen": issue.get("firstSeen"),
        "lastSeen": issue.get("lastSeen"),
        "permalink": issue.get("permalink"),
    }


def issues_to_message(issues: list[dict]) -> str:
    return "/sentry-sweep " + json.dumps(issues, separators=(",", ":"))


def sweep_name(now: datetime | None = None) -> str:
    return "sweep-" + (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")


def build_create_command(name: str, message: str) -> list[str]:
    # The sweep runs on its own fresh host (--provider/--new-host): the cron
    # container dies as soon as tick() returns, so a default (local) agent
    # would be killed mid-triage — and --idle-timeout is disabled for local
    # agents anyway (mngr's --idle-mode default is "disabled if local").
    provider = os.environ.get("OPEN_SEER_SWEEP_PROVIDER", "modal")
    cmd = [
        "mngr",
        "create",
        name,
        "--provider",
        provider,
        "--new-host",
        "--headless",
        "--no-connect",
        "--yes",
        "--idle-timeout",
        SWEEP_IDLE_TIMEOUT,
        "-b",
        f"--timeout={SWEEP_SANDBOX_TIMEOUT_SECONDS}",
    ]
    for var in SWEEP_PASS_ENV:
        cmd += ["--pass-env", var]
    cmd += ["--message", message]
    return cmd


def parse_agent_list(text: str) -> list[dict]:
    """Parse `mngr list --format json` output (empty list prints nothing)."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    # tolerate jsonl-shaped output
    except json.JSONDecodeError:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        data = data.get("agents", [data])
    return data


def has_running_sweep(agents: list[dict]) -> bool:
    return any(
        str(agent.get("name", "")).startswith("sweep-") and agent.get("state") in RUNNING_STATES for agent in agents
    )


# --- Mirror sync (§2: never delete) --------------------------------------


def _repo_url(repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def mirror_sync(source_repo: str, mirror_repo: str, token: str, dry_run: bool = False) -> None:
    """Push main + all new/updated branches to the mirror; never delete."""
    with tempfile.TemporaryDirectory(prefix="open-seer-mirror-") as tmp:
        subprocess.run(["git", "init", "--bare", "--quiet", tmp], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                tmp,
                "fetch",
                "--quiet",
                _repo_url(source_repo, token),
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            check=True,
        )
        push_cmd = ["git", "-C", tmp, "push", "--quiet", _repo_url(mirror_repo, token), MIRROR_PUSH_REFSPEC]
        if dry_run:
            log.info("DRY RUN (mirror push skipped): %s", _redact(shlex.join(push_cmd), token))
        else:
            subprocess.run(push_cmd, check=True)
            log.info("mirror sync: %s -> %s", source_repo, mirror_repo)


# --- The tick ------------------------------------------------------------


def tick() -> int:
    # (a) Kill switch: flip the secret, nothing new spawns from the next tick.
    if not _truthy(os.environ.get("OPEN_SEER_ENABLED")):
        return 0
    dry_run = _truthy(os.environ.get("OPEN_SEER_DRY_RUN"))

    # (b) Mirror sync runs every tick; failures never block sweep dispatch.
    github_token = os.environ.get("GITHUB_TOKEN", "")
    try:
        mirror_sync(
            os.environ.get("MIRROR_SOURCE_REPO", "imbue-ai/mngr"),
            os.environ.get("MIRROR_REPO", "imbue-ai/agentic-mngr"),
            github_token,
            dry_run=dry_run,
        )
    # deliberately broad: log and move on
    except Exception as exc:
        log.error("mirror sync failed (sweep dispatch continues): %s", _redact(str(exc), github_token))

    # (c) Unassigned + unresolved error issues across minds-* projects.
    org = os.environ["SENTRY_ORG"]
    sentry_token = os.environ["SENTRY_AUTH_TOKEN"]
    prefix = os.environ.get("SENTRY_PROJECT_PREFIX", "minds-")
    projects = filter_projects(fetch_projects(org, sentry_token), prefix)
    issues = fetch_issues(org, projects, sentry_token)
    if not issues:
        # (d) nothing to do — no agent runs, no tokens spent
        return 0

    # (e) Overlap guard; fail closed if we cannot tell. --safe forces
    # provider-side discovery (each tick runs in a fresh container, so a
    # sweep spawned by an earlier tick is only visible by querying the
    # provider); --on-error abort + check=True turn partial discovery
    # failures into an exception instead of a silently empty roster.
    try:
        listing = subprocess.run(
            ["mngr", "list", "--format", "json", "--headless", "--safe", "--on-error", "abort"],
            capture_output=True,
            text=True,
            check=True,
        )
        agents = parse_agent_list(listing.stdout)
    except Exception as exc:
        log.error("mngr list failed; not spawning a sweep this tick: %s", exc)
        return 0
    if has_running_sweep(agents):
        log.error(
            "previous sweep still running; skipping spawn (%d issue(s) wait for the next tick)",
            len(issues),
        )
        return 0

    # (f) Spawn the sweep with the prompt baked in. Dry-run still spawns:
    # spawning is not one of the writes §11 suppresses — OPEN_SEER_DRY_RUN
    # is forwarded (--pass-env) so the sweep itself runs read-only and
    # prints intended actions to its transcript (DESIGN §11).
    cmd = build_create_command(sweep_name(), issues_to_message([compact_issue(i) for i in issues]))
    subprocess.run(cmd, check=True)
    log.info("spawned %s with %d issue(s)%s", cmd[2], len(issues), " [dry-run sweep]" if dry_run else "")
    return 0


# --- Modal wrapper (lazy: tests and local ticks never need modal) ---------

try:
    import modal
except ImportError:
    _HAS_MODAL = False
else:
    _HAS_MODAL = True

if _HAS_MODAL:
    app = modal.App("open-seer")
    image = modal.Image.from_dockerfile("Dockerfile")

    @app.function(
        image=image,
        schedule=modal.Cron("0 * * * *"),
        secrets=[modal.Secret.from_name("open-seer")],
        timeout=30 * 60,
    )
    def hourly_tick() -> None:
        logging.basicConfig(level=logging.INFO)
        tick()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(tick())
