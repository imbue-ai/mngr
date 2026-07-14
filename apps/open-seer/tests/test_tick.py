"""Pure-python tests for the open-seer tick — no network, no Modal, no mngr.

Covers the deterministic surface DESIGN.md §11 calls out: query
construction, project-prefix filtering, the kill switch, the overlap
guard, issue-list-to-message serialization, and the never-delete
guarantee of the mirror-sync push.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from datetime import timezone

import pytest
import tick

# --- helpers ---------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, links=None):
        self._payload = payload
        self.links = links or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def completed(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


def boom(*args, **kwargs):
    raise AssertionError("must not be called")


RAW_ISSUE = {
    "shortId": "MINDS-API-1",
    "id": "5678",
    "title": "TypeError: NoneType has no attribute 'user'",
    "culprit": "minds/api/session.py in get_user",
    "level": "error",
    "count": "42",
    "firstSeen": "2026-07-09T00:00:00Z",
    "lastSeen": "2026-07-10T00:00:00Z",
    "permalink": "https://imbue.sentry.io/issues/5678/",
    "project": {"id": "11", "slug": "minds-api", "name": "minds-api"},
    # noise that must not reach the message
    "stats": {"24h": [[0, 1]] * 24},
    "assignedTo": None,
}


@pytest.fixture
def enabled_env(monkeypatch):
    monkeypatch.setenv("OPEN_SEER_ENABLED", "1")
    monkeypatch.delenv("OPEN_SEER_DRY_RUN", raising=False)
    monkeypatch.setenv("SENTRY_ORG", "imbue")
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sentry-secret")
    monkeypatch.setenv("SENTRY_PROJECT_PREFIX", "minds-")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")


def wire_happy_path(monkeypatch, issues=None):
    """Stub mirror + Sentry so tick() reaches the mngr steps."""
    monkeypatch.setattr(tick, "mirror_sync", lambda *a, **k: None)
    monkeypatch.setattr(tick, "fetch_projects", lambda org, token: [{"id": "11", "slug": "minds-api"}])
    monkeypatch.setattr(
        tick,
        "fetch_issues",
        lambda org, projects, token: [RAW_ISSUE] if issues is None else issues,
    )


# --- query construction ------------------------------------------------------


def test_issue_query_tokens():
    assert "is:unresolved" in tick.ISSUE_QUERY
    assert "is:unassigned" in tick.ISSUE_QUERY
    assert "issue.category:error" in tick.ISSUE_QUERY
    assert "level:[error,fatal]" in tick.ISSUE_QUERY


def test_issue_query_never_touches_regressions():
    # Regressed issues are a human's job (DESIGN.md §8) — never queried.
    assert "regressed" not in tick.ISSUE_QUERY


def test_fetch_issues_queries_each_project(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, headers, params))
        return FakeResponse([dict(RAW_ISSUE)])

    monkeypatch.setattr(tick.requests, "get", fake_get)
    issues = tick.fetch_issues("imbue", [{"id": "11"}, {"id": "22"}], "tok")

    assert len(issues) == 2
    assert [url for url, _, _ in calls] == ["https://sentry.io/api/0/organizations/imbue/issues/"] * 2
    assert [params["project"] for _, _, params in calls] == ["11", "22"]
    assert all(params["query"] == tick.ISSUE_QUERY for _, _, params in calls)
    assert all(headers["Authorization"] == "Bearer tok" for _, headers, _ in calls)


def test_fetch_projects_hits_org_projects_endpoint(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        return FakeResponse([{"id": "11", "slug": "minds-api"}])

    monkeypatch.setattr(tick.requests, "get", fake_get)
    projects = tick.fetch_projects("imbue", "tok")
    assert calls == ["https://sentry.io/api/0/organizations/imbue/projects/"]
    assert projects[0]["slug"] == "minds-api"


def test_pagination_follows_link_header(monkeypatch):
    pages = [
        FakeResponse([{"id": "1"}], links={"next": {"url": "https://sentry.io/page2", "results": "true"}}),
        FakeResponse([{"id": "2"}], links={"next": {"url": "https://sentry.io/page3", "results": "false"}}),
    ]

    def fake_get(url, headers=None, params=None, timeout=None):
        return pages.pop(0)

    monkeypatch.setattr(tick.requests, "get", fake_get)
    results = tick._sentry_get_paginated("/organizations/imbue/projects/", "tok", {})
    assert [r["id"] for r in results] == ["1", "2"]


# --- project-prefix filtering -------------------------------------------------


def test_filter_projects_by_prefix():
    projects = [
        {"id": "1", "slug": "minds-api"},
        {"id": "2", "slug": "minds-web"},
        {"id": "3", "slug": "other-app"},
        # prefix must anchor at the start
        {"id": "4", "slug": "reminds-api"},
        # no slug at all
        {"id": "5"},
    ]
    kept = tick.filter_projects(projects, "minds-")
    assert [p["slug"] for p in kept] == ["minds-api", "minds-web"]


# --- kill switch --------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off"])
def test_kill_switch_exits_silently(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("OPEN_SEER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("OPEN_SEER_ENABLED", value)
    monkeypatch.setattr(tick, "mirror_sync", boom)
    monkeypatch.setattr(tick, "fetch_projects", boom)
    monkeypatch.setattr(tick.subprocess, "run", boom)

    assert tick.tick() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_kill_switch_truthy_values(value):
    assert tick._truthy(value)


# --- overlap guard -------------------------------------------------------------


def test_running_sweep_detected():
    assert tick.has_running_sweep([{"name": "sweep-20260710120000", "state": "RUNNING"}])


def test_waiting_and_unknown_sweeps_count_as_running():
    assert tick.has_running_sweep([{"name": "sweep-1", "state": "WAITING"}])
    assert tick.has_running_sweep([{"name": "sweep-1", "state": "UNKNOWN"}])


def test_stopped_or_done_sweeps_do_not_block():
    agents = [
        {"name": "sweep-20260710110000", "state": "STOPPED"},
        {"name": "sweep-20260710100000", "state": "DONE"},
    ]
    assert not tick.has_running_sweep(agents)


def test_running_fixers_do_not_block():
    assert not tick.has_running_sweep([{"name": "fixer-MINDS-API-1", "state": "RUNNING"}])


def test_empty_roster_does_not_block():
    assert not tick.has_running_sweep([])


def test_parse_agent_list_variants():
    assert tick.parse_agent_list("") == []
    assert tick.parse_agent_list('[{"name": "sweep-1", "state": "RUNNING"}]') == [
        {"name": "sweep-1", "state": "RUNNING"}
    ]
    jsonl = '{"name": "a", "state": "STOPPED"}\n{"name": "b", "state": "RUNNING"}\n'
    assert [a["name"] for a in tick.parse_agent_list(jsonl)] == ["a", "b"]


def test_tick_skips_spawn_and_logs_error_on_overlap(enabled_env, monkeypatch, caplog):
    wire_happy_path(monkeypatch)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        assert cmd[:2] == ["mngr", "list"], f"unexpected subprocess call: {cmd}"
        return completed(json.dumps([{"name": "sweep-20260710110000", "state": "RUNNING"}]))

    monkeypatch.setattr(tick.subprocess, "run", fake_run)
    with caplog.at_level(logging.ERROR, logger="open-seer"):
        assert tick.tick() == 0

    assert any("still running" in record.message for record in caplog.records)
    assert not any(cmd[:2] == ["mngr", "create"] for cmd in commands)


def test_tick_fails_closed_when_mngr_list_errors(enabled_env, monkeypatch, caplog):
    wire_happy_path(monkeypatch)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(tick.subprocess, "run", fake_run)
    with caplog.at_level(logging.ERROR, logger="open-seer"):
        assert tick.tick() == 0

    assert any("not spawning" in record.message for record in caplog.records)
    assert not any(cmd[:2] == ["mngr", "create"] for cmd in commands)


# --- issue list -> sweep message -----------------------------------------------


def test_compact_issue_keeps_contract_fields_and_drops_noise():
    compact = tick.compact_issue(RAW_ISSUE)
    assert compact == {
        "shortId": "MINDS-API-1",
        "id": "5678",
        "project": "minds-api",
        "title": "TypeError: NoneType has no attribute 'user'",
        "culprit": "minds/api/session.py in get_user",
        "level": "error",
        "count": "42",
        "firstSeen": "2026-07-09T00:00:00Z",
        "lastSeen": "2026-07-10T00:00:00Z",
        "permalink": "https://imbue.sentry.io/issues/5678/",
    }


def test_issues_to_message_round_trips():
    issues = [tick.compact_issue(RAW_ISSUE)]
    message = tick.issues_to_message(issues)
    assert message.startswith("/sentry-sweep ")
    payload = json.loads(message[len("/sentry-sweep ") :])
    assert payload == issues


def test_issues_to_message_is_compact():
    message = tick.issues_to_message([{"shortId": "MINDS-API-1", "id": "5678"}])
    assert message == '/sentry-sweep [{"shortId":"MINDS-API-1","id":"5678"}]'


def test_sweep_name_is_utc_timestamp():
    now = datetime(2026, 7, 10, 12, 34, 56, tzinfo=timezone.utc)
    assert tick.sweep_name(now) == "sweep-20260710123456"


def test_build_create_command_shape(monkeypatch):
    monkeypatch.delenv("OPEN_SEER_SWEEP_PROVIDER", raising=False)
    cmd = tick.build_create_command("sweep-20260710123456", "/sentry-sweep []")
    assert cmd[:3] == ["mngr", "create", "sweep-20260710123456"]
    # Its own host: the cron container dies as soon as the tick returns.
    assert cmd[cmd.index("--provider") + 1] == "modal"
    assert "--new-host" in cmd
    # finished sweeps self-stop (§2)
    assert "--idle-timeout" in cmd
    # Raise the modal sandbox's hard max lifetime past the 15-min default.
    assert cmd[cmd.index("-b") + 1] == f"--timeout={tick.SWEEP_SANDBOX_TIMEOUT_SECONDS}"
    # cron has no terminal to attach
    assert "--no-connect" in cmd
    # The fresh host inherits nothing — the skill's required env is forwarded.
    passed = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--pass-env"]
    assert passed == list(tick.SWEEP_PASS_ENV)
    for var in ("SENTRY_AUTH_TOKEN", "GITHUB_TOKEN", "TARGET_REPO", "OPEN_SEER_DRY_RUN"):
        assert var in passed
    assert cmd[-2:] == ["--message", "/sentry-sweep []"]


def test_build_create_command_provider_override(monkeypatch):
    monkeypatch.setenv("OPEN_SEER_SWEEP_PROVIDER", "docker")
    cmd = tick.build_create_command("sweep-1", "/sentry-sweep []")
    assert cmd[cmd.index("--provider") + 1] == "docker"


# --- spawn path ------------------------------------------------------------------


def make_mngr_runner(commands):
    """Fake subprocess.run: empty agent roster, records every command."""

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[:2] == ["mngr", "list"]:
            # mngr prints nothing for an empty roster
            return completed("")
        return completed()

    return fake_run


def test_tick_spawns_sweep(enabled_env, monkeypatch):
    wire_happy_path(monkeypatch)
    commands = []
    monkeypatch.setattr(tick.subprocess, "run", make_mngr_runner(commands))

    assert tick.tick() == 0

    # Overlap guard: --safe forces provider-side discovery (fresh container
    # each tick), --on-error abort fails closed on partial discovery.
    lists = [cmd for cmd in commands if cmd[:2] == ["mngr", "list"]]
    assert len(lists) == 1
    assert "--safe" in lists[0]
    assert lists[0][lists[0].index("--on-error") + 1] == "abort"

    creates = [cmd for cmd in commands if cmd[:2] == ["mngr", "create"]]
    assert len(creates) == 1
    name, message = creates[0][2], creates[0][-1]
    assert name.startswith("sweep-") and name[len("sweep-") :].isdigit()
    assert message.startswith("/sentry-sweep ")
    assert json.loads(message[len("/sentry-sweep ") :])[0]["shortId"] == "MINDS-API-1"


def test_tick_exits_silently_with_zero_issues(enabled_env, monkeypatch, capsys):
    wire_happy_path(monkeypatch, issues=[])
    # no mngr list, no spawn
    monkeypatch.setattr(tick.subprocess, "run", boom)

    assert tick.tick() == 0
    assert capsys.readouterr().out == ""


def test_dry_run_still_spawns_sweep_and_forwards_the_switch(enabled_env, monkeypatch):
    """DESIGN §11: dry-run is a sweep-level mode — the tick still spawns the
    sweep (spawning is not a Sentry/GitHub write) and forwards
    OPEN_SEER_DRY_RUN so the sweep prints intended writes to its transcript."""
    monkeypatch.setenv("OPEN_SEER_DRY_RUN", "1")
    wire_happy_path(monkeypatch)
    commands = []
    monkeypatch.setattr(tick.subprocess, "run", make_mngr_runner(commands))

    assert tick.tick() == 0

    creates = [cmd for cmd in commands if cmd[:2] == ["mngr", "create"]]
    assert len(creates) == 1
    assert creates[0][2].startswith("sweep-")
    passed = [creates[0][i + 1] for i, arg in enumerate(creates[0]) if arg == "--pass-env"]
    assert "OPEN_SEER_DRY_RUN" in passed


def test_mirror_failure_does_not_block_dispatch(enabled_env, monkeypatch, caplog):
    def failing_mirror(*args, **kwargs):
        raise RuntimeError("push to https://x-access-token:gh-secret@github.com failed")

    monkeypatch.setattr(tick, "mirror_sync", failing_mirror)
    monkeypatch.setattr(tick, "fetch_projects", lambda org, token: [{"id": "11", "slug": "minds-api"}])
    monkeypatch.setattr(tick, "fetch_issues", lambda org, projects, token: [RAW_ISSUE])
    commands = []
    monkeypatch.setattr(tick.subprocess, "run", make_mngr_runner(commands))

    with caplog.at_level(logging.ERROR, logger="open-seer"):
        assert tick.tick() == 0

    # dispatch went ahead
    assert any(cmd[:2] == ["mngr", "create"] for cmd in commands)
    assert any("mirror sync failed" in record.message for record in caplog.records)
    # token never reaches the logs
    assert "gh-secret" not in caplog.text


# --- mirror sync: never delete -----------------------------------------------------


def has_deletion_refspec(cmd: list[str]) -> bool:
    """A deletion refspec has an empty source side, e.g. ':refs/heads/x'."""
    return any(arg.startswith(":") for arg in cmd)


def test_mirror_push_refspec_never_deletes():
    source, _, dest = tick.MIRROR_PUSH_REFSPEC.partition(":")
    # non-empty source: updates/creates, never deletes
    assert source.lstrip("+")
    assert dest.startswith("refs/heads/")


def test_mirror_sync_commands_never_delete(monkeypatch):
    commands = []
    monkeypatch.setattr(tick.subprocess, "run", lambda cmd, **k: (commands.append(cmd), completed())[1])

    tick.mirror_sync("imbue-ai/mngr", "imbue-ai/agentic-mngr", "tok")

    pushes = [cmd for cmd in commands if "push" in cmd]
    assert len(pushes) == 1
    assert tick.MIRROR_PUSH_REFSPEC in pushes[0]
    assert any("imbue-ai/agentic-mngr" in arg for arg in pushes[0])
    for cmd in commands:
        assert "--prune" not in cmd
        assert "--mirror" not in cmd
        assert "--delete" not in cmd and "-d" not in cmd
        assert not has_deletion_refspec(cmd)


def test_mirror_sync_dry_run_skips_push_and_redacts_token(monkeypatch, caplog):
    commands = []
    monkeypatch.setattr(tick.subprocess, "run", lambda cmd, **k: (commands.append(cmd), completed())[1])

    with caplog.at_level(logging.INFO, logger="open-seer"):
        tick.mirror_sync("imbue-ai/mngr", "imbue-ai/agentic-mngr", "gh-secret", dry_run=True)

    # fetch only
    assert not any("push" in cmd for cmd in commands)
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "DRY RUN" in messages and "gh-secret" not in messages
