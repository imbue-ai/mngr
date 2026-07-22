"""Tests for the workspace-side backup scripts, run for real against temp git repos.

The scripts are executed exactly as they are on a workspace -- through the
base64 shell command via ``bash -c`` -- with a stub ``uv`` / ``supervisorctl``
placed on PATH so the mngr-list gate and the service restart behave like a
healthy (or deliberately broken) workspace without any real infrastructure.
The restore-script tests additionally run against a real local restic repo.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_APPLY_UPDATE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_CHECK_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_GATE_PROBE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_RESTORE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import CHECK_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import GATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import OFFICIAL_REMOTE_URL
from imbue.minds.desktop_client.backup_workspace_scripts import RESTORE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import UPDATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import build_workspace_script_command
from imbue.minds.desktop_client.backup_workspace_scripts import extract_marker_json
from imbue.minds.desktop_client.restic_cli import _get_restic_binary
from imbue.minds.testing import run_git_for_backup_test
from imbue.minds.testing import tag_newer_release_content
from imbue.minds.testing import write_stub_supervisorctl


def _make_workspace_repo(tmp_path: Path) -> Path:
    """A git repo shaped like a workspace: libs/host_backup + a tagged version."""
    repo = tmp_path / "workspace"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, timeout=60)
    backup_dir = repo / "libs" / "host_backup"
    backup_dir.mkdir(parents=True)
    (backup_dir / "service.py").write_text("VERSION = 1\n")
    (repo / "other.txt").write_text("unrelated\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "initial")
    return repo


def _run_script(
    repo: Path,
    script: str,
    args: tuple[str, ...],
    *,
    extra_path: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> dict:
    command = build_workspace_script_command(script, args)
    env = dict(os.environ)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    env.pop("MNGR_AGENT_STATE_DIR", None)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        ["bash", "-c", command], cwd=repo, capture_output=True, text=True, check=False, timeout=300, env=env
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}


def _make_stub_bin(
    tmp_path: Path,
    *,
    agents_json: str = '{"agents": [], "errors": []}',
    restart_ok: bool = True,
    sync_ok: bool = True,
    supervisorctl_call_log: Path | None = None,
    supervisorctl_hook: str = "",
) -> Path:
    """A PATH dir with stub `uv` and `supervisorctl` acting like a healthy workspace.

    ``sync_ok=False`` fails `uv sync` (a post-swap failpoint for the restore
    script); ``supervisorctl_call_log`` / ``supervisorctl_hook`` are forwarded
    to the supervisorctl stub for lifecycle-order assertions and deterministic
    race injection.
    """
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    uv_stub = stub_bin / "uv"
    uv_stub.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "run" ] && [ "$2" = "mngr" ] && [ "$3" = "list" ]; then\n'
        f"  echo '{agents_json}'\n"
        "  exit 0\n"
        "fi\n"
        + ("" if sync_ok else 'if [ "$1" = "sync" ]; then echo "injected uv sync failure" >&2; exit 1; fi\n')
        + "exit 0\n"
    )
    uv_stub.chmod(0o755)
    write_stub_supervisorctl(
        stub_bin, is_restart_ok=restart_ok, call_log_path=supervisorctl_call_log, hook_script=supervisorctl_hook
    )
    return stub_bin


def _running_chat_agents_json(repo: Path) -> str:
    agents = [
        {"name": "chat-1", "id": "agent-1", "type": "claude", "state": "RUNNING", "work_dir": str(repo)},
        {"name": "services", "id": "agent-2", "type": "main", "state": "RUNNING", "work_dir": str(repo)},
        {"name": "worker-1", "id": "agent-3", "type": "worker", "state": "RUNNING", "work_dir": str(repo / "wt")},
    ]
    return json.dumps({"agents": agents, "errors": []})


# --- marker/command plumbing ---


def test_module_official_url_constant_matches_the_script_default() -> None:
    # The module-level constant (used for display / docs) and the default baked
    # into the script preamble must never drift apart.
    assert f'DEFAULT_OFFICIAL_REMOTE_URL = "{OFFICIAL_REMOTE_URL}"' in BACKUP_CHECK_SCRIPT


def test_extract_marker_json_finds_last_payload_amid_noise() -> None:
    stdout = 'warning: something\nMARKER:{"a": 1}\nnoise\nMARKER:{"a": 2}\ntrailing\n'
    assert extract_marker_json(stdout, "MARKER:") == {"a": 2}


def test_extract_marker_json_returns_none_without_marker() -> None:
    assert extract_marker_json("no marker here", "MARKER:") is None


def test_extract_marker_json_returns_none_for_bad_json() -> None:
    assert extract_marker_json("MARKER:{broken", "MARKER:") is None


def test_build_workspace_script_command_round_trips_through_bash(tmp_path: Path) -> None:
    script = 'import sys\nprint("OUT:" + " ".join(sys.argv[1:]))\n'
    command = build_workspace_script_command(script, ("--flag", "value with spaces"))
    result = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=True, timeout=60, cwd=tmp_path
    )
    assert "OUT:--flag value with spaces" in result.stdout


# --- check script against real git repos ---


def test_check_script_reports_matches_when_tag_equals_worktree(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minimum-tag", "minds-v1.0.0"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["target_tag"] == "minds-v1.0.0"
    assert payload["code_state"] == "matches"
    assert payload["service_state"] == "running"
    assert payload["env"] == {"present": False}


def test_check_script_reports_newer_when_tag_is_ancestor(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    (repo / "libs" / "host_backup" / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "local improvement on top of the tag")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minimum-tag", "minds-v1.0.0"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["code_state"] == "newer"


def test_check_script_reports_outdated_when_tag_is_not_an_ancestor(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    # The tag lives on a side branch ahead of main: main's content differs and
    # does not contain the tag -> outdated.
    tag_newer_release_content(repo)
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minimum-tag", "minds-v2.0.0"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["code_state"] == "outdated"


def test_check_script_reports_unverifiable_when_minimum_tag_is_missing(tmp_path: Path) -> None:
    # The minimum tag has no highest-tag fallback: missing after a (failed)
    # fetch from the official remote means the check cannot run.
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(
        repo,
        BACKUP_CHECK_SCRIPT,
        ("--minimum-tag", "minds-v9.9.9", "--official-url", str(tmp_path / "nonexistent-remote")),
        extra_path=stub_bin,
    )
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["code_state"] == "unverifiable"
    assert payload["target_tag"] == "minds-v9.9.9"


def test_check_script_fetches_missing_minimum_tag_from_the_official_remote(tmp_path: Path) -> None:
    # The workspace lacks the minimum tag locally; the script must add the
    # `official` remote pointing at the given URL and fetch the tag from it.
    template_parent = tmp_path / "template-parent"
    template_parent.mkdir()
    template = _make_workspace_repo(template_parent)
    run_git_for_backup_test(template, "tag", "minds-v1.0.0")
    repo = tmp_path / "workspace-clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-tags", str(template), str(repo)], check=True, capture_output=True, timeout=60
    )
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(
        repo,
        BACKUP_CHECK_SCRIPT,
        ("--minimum-tag", "minds-v1.0.0", "--official-url", str(template)),
        extra_path=stub_bin,
    )
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["code_state"] == "matches"
    remote_url = subprocess.run(
        ["git", "remote", "get-url", "official"], cwd=repo, capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()
    assert remote_url == str(template)


def test_check_script_repoints_a_wrong_official_remote(tmp_path: Path) -> None:
    # minds owns the `official` remote name: an existing remote pointing
    # elsewhere is idempotently repointed at the given URL.
    template_parent = tmp_path / "template-parent"
    template_parent.mkdir()
    template = _make_workspace_repo(template_parent)
    run_git_for_backup_test(template, "tag", "minds-v1.0.0")
    repo = tmp_path / "workspace-clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-tags", str(template), str(repo)], check=True, capture_output=True, timeout=60
    )
    subprocess.run(
        ["git", "remote", "add", "official", str(tmp_path / "somewhere-else")],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=60,
    )
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(
        repo,
        BACKUP_CHECK_SCRIPT,
        ("--minimum-tag", "minds-v1.0.0", "--official-url", str(template)),
        extra_path=stub_bin,
    )
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["code_state"] == "matches"
    remote_url = subprocess.run(
        ["git", "remote", "get-url", "official"], cwd=repo, capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()
    assert remote_url == str(template)


def test_check_script_accepts_installed_identity_at_or_above_the_minimum(tmp_path: Path) -> None:
    # A workspace updated by content commit (`backup-update: minds-v2.0.0`)
    # never gains the minimum tag as an ancestor; the installed identity at or
    # above the minimum must still read as fine.
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    (repo / "libs" / "host_backup" / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "backup-update: minds-v2.0.0")
    # Orphan the minimum tag onto a side commit so it is NOT an ancestor.
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    (repo / "side.txt").write_text("side\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "side content")
    run_git_for_backup_test(repo, "tag", "-f", "minds-v1.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minimum-tag", "minds-v1.0.0"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["installed_version"] == "minds-v2.0.0"
    assert payload["code_state"] == "newer"


def test_check_script_reports_env_sha_and_content(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    env_path = repo / "runtime" / "secrets" / "restic.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minimum-tag", "minds-v1.0.0"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    env_info = payload["env"]
    assert isinstance(env_info, dict)
    env_map: dict[str, object] = {str(key): value for key, value in env_info.items()}
    assert env_map["present"] is True
    sha_value = env_map["sha256"]
    assert isinstance(sha_value, str) and len(sha_value) == 64
    assert "content_b64" in env_map


# --- gate probe script ---


def test_gate_probe_reports_running_chats_excluding_main_and_worktrees(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    stub_bin = _make_stub_bin(tmp_path, agents_json=_running_chat_agents_json(repo))
    run = _run_script(repo, BACKUP_GATE_PROBE_SCRIPT, ("--agent-id", "agent-x"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], GATE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["running_chats"] == ["chat-1"]
    assert payload["backup_tick_in_flight"] is False


def test_gate_probe_detects_in_flight_backup_tick(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    host_dir = tmp_path / "host"
    events_path = host_dir / "agents" / "agent-x" / "events" / "backup" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    events_path.write_text(
        json.dumps({"type": "BACKUP_STARTED", "tick_id": "t1"})
        + "\n"
        + json.dumps({"type": "RESTIC_BACKUP_SUCCEEDED", "tick_id": "t1"})
        + "\n"
        + json.dumps({"type": "BACKUP_STARTED", "tick_id": "t2"})
        + "\n"
    )
    stub_bin = _make_stub_bin(tmp_path)
    command = build_workspace_script_command(BACKUP_GATE_PROBE_SCRIPT, ("--agent-id", "agent-x"))
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["MNGR_HOST_DIR"] = str(host_dir)
    env.pop("MNGR_AGENT_STATE_DIR", None)
    result = subprocess.run(
        ["bash", "-c", command], cwd=repo, capture_output=True, text=True, check=False, timeout=120, env=env
    )
    payload = extract_marker_json(result.stdout, GATE_RESULT_MARKER)
    assert payload is not None, result.stdout + result.stderr
    assert payload["backup_tick_in_flight"] is True


def test_gate_probe_ignores_a_stale_dead_tick_once_a_newer_tick_finished(tmp_path: Path) -> None:
    # A tick killed mid-flight (e.g. by a service restart) never writes its
    # completion event. Once a newer tick has started and finished, the dead
    # tick must not read as in flight -- ticks run serially, so only the most
    # recently started tick can be.
    repo = _make_workspace_repo(tmp_path)
    host_dir = tmp_path / "host"
    events_path = host_dir / "agents" / "agent-x" / "events" / "backup" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    events_path.write_text(
        json.dumps({"type": "BACKUP_STARTED", "tick_id": "dead-tick"})
        + "\n"
        + json.dumps({"type": "BACKUP_STARTED", "tick_id": "t2"})
        + "\n"
        + json.dumps({"type": "RESTIC_BACKUP_SUCCEEDED", "tick_id": "t2"})
        + "\n"
    )
    stub_bin = _make_stub_bin(tmp_path)
    command = build_workspace_script_command(BACKUP_GATE_PROBE_SCRIPT, ("--agent-id", "agent-x"))
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["MNGR_HOST_DIR"] = str(host_dir)
    env.pop("MNGR_AGENT_STATE_DIR", None)
    result = subprocess.run(
        ["bash", "-c", command], cwd=repo, capture_output=True, text=True, check=False, timeout=120, env=env
    )
    payload = extract_marker_json(result.stdout, GATE_RESULT_MARKER)
    assert payload is not None, result.stdout + result.stderr
    assert payload["backup_tick_in_flight"] is False


# --- apply update script ---


def test_apply_update_commits_tag_content_and_restores_stash(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    # The target tag carries newer backup code on a side branch (outdated state).
    tag_newer_release_content(repo)
    # Uncommitted user work that must survive the update via the stash.
    (repo / "other.txt").write_text("user edit in progress\n")
    (repo / "untracked.txt").write_text("scratch\n")

    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(
        repo, BACKUP_APPLY_UPDATE_SCRIPT, ("--minds-version", "2.0.0", "--agent-id", "agent-x"), extra_path=stub_bin
    )
    payload = extract_marker_json(run["stdout"], UPDATE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "ok", payload
    assert payload["committed"] is True
    assert payload["stashed"] is True
    assert payload["stash_conflict"] is False
    # The backup code now matches the tag and is committed with the convention subject.
    assert (repo / "libs" / "host_backup" / "service.py").read_text() == "VERSION = 2\n"
    subject = run_git_for_backup_test(repo, "log", "-1", "--format=%s").strip()
    assert subject == "backup-update: minds-v2.0.0"
    # The user's uncommitted work came back.
    assert (repo / "other.txt").read_text() == "user edit in progress\n"
    assert (repo / "untracked.txt").read_text() == "scratch\n"


def test_apply_update_removes_files_deleted_in_the_target_tag(tmp_path: Path) -> None:
    # A plain `git checkout <tag> -- <path>` never deletes files the tag
    # removed; the update must still converge to exactly the tag's content.
    repo = _make_workspace_repo(tmp_path)
    (repo / "libs" / "host_backup" / "stale.py").write_text("OBSOLETE = True\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "module the next release removes")
    tag_newer_release_content(repo, removed_file="libs/host_backup/stale.py")

    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(
        repo, BACKUP_APPLY_UPDATE_SCRIPT, ("--minds-version", "2.0.0", "--agent-id", "agent-x"), extra_path=stub_bin
    )
    payload = extract_marker_json(run["stdout"], UPDATE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "ok", payload
    assert payload["committed"] is True
    assert not (repo / "libs" / "host_backup" / "stale.py").exists()
    assert (repo / "libs" / "host_backup" / "service.py").read_text() == "VERSION = 2\n"
    # The content now matches the tag exactly, so a re-check reads clean.
    diffed = subprocess.run(
        ["git", "diff", "--quiet", "minds-v2.0.0", "--", "libs/host_backup"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert diffed.returncode == 0


def test_apply_update_is_blocked_by_running_chats_without_stop_flag(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    stub_bin = _make_stub_bin(tmp_path, agents_json=_running_chat_agents_json(repo))
    run = _run_script(
        repo, BACKUP_APPLY_UPDATE_SCRIPT, ("--minds-version", "1.0.0", "--agent-id", "agent-x"), extra_path=stub_bin
    )
    payload = extract_marker_json(run["stdout"], UPDATE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "blocked"
    assert payload["running_chats"] == ["chat-1"]


def test_apply_update_rolls_back_when_service_restart_fails(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    tag_newer_release_content(repo)
    pre_content = (repo / "libs" / "host_backup" / "service.py").read_text()

    stub_bin = _make_stub_bin(tmp_path, restart_ok=False)
    run = _run_script(
        repo, BACKUP_APPLY_UPDATE_SCRIPT, ("--minds-version", "2.0.0", "--agent-id", "agent-x"), extra_path=stub_bin
    )
    payload = extract_marker_json(run["stdout"], UPDATE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "failed"
    assert payload["rolled_back"] is True
    # The restore restart also fails here (the stub always fails restarts), and
    # that must be visible in the detail rather than silently swallowed.
    detail = payload["detail"]
    assert isinstance(detail, str)
    assert "restoring the service failed" in detail
    # The revert restored the pre-update content, keeping both commits in history.
    assert (repo / "libs" / "host_backup" / "service.py").read_text() == pre_content
    subjects = run_git_for_backup_test(repo, "log", "--format=%s").splitlines()
    assert subjects[0].startswith('Revert "backup-update: minds-v2.0.0"')
    assert subjects[1] == "backup-update: minds-v2.0.0"
    # The reverted update must not read as the installed version: the check
    # skips the reverted `backup-update:` subject and (here) falls back to an
    # empty identity, since the tag is not an ancestor of HEAD.
    check_run = _run_script(
        repo, BACKUP_CHECK_SCRIPT, ("--minimum-tag", "minds-v2.0.0", "--agent-id", "agent-x"), extra_path=stub_bin
    )
    check_payload = extract_marker_json(check_run["stdout"], CHECK_RESULT_MARKER)
    assert check_payload is not None, check_run
    assert check_payload["installed_version"] == ""


def test_apply_update_skips_commit_when_content_already_matches(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(
        repo, BACKUP_APPLY_UPDATE_SCRIPT, ("--minds-version", "1.0.0", "--agent-id", "agent-x"), extra_path=stub_bin
    )
    payload = extract_marker_json(run["stdout"], UPDATE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "ok"
    assert payload["committed"] is False
    assert run_git_for_backup_test(repo, "log", "-1", "--format=%s").strip() == "initial"


# --- restore script against a real local restic repo ---

_RESTIC_TEST_PASSWORD = "restore-test-password"


def _restic_for_test(restic_repo: Path, *args: str) -> str:
    """Run real restic against the test repo; return stdout."""
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": str(restic_repo), "RESTIC_PASSWORD": _RESTIC_TEST_PASSWORD})
    result = subprocess.run(
        [_get_restic_binary(), *args], capture_output=True, text=True, check=True, timeout=120, env=env
    )
    return result.stdout


def _make_restore_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A host dir shaped like /mngr (code repo + restic.env) and an initialized local restic repo.

    Returns (host_dir, code_dir, restic_repo). ``resolve()``d paths so the
    snapshot's recorded absolute path matches what the script computes via
    realpath (macOS tmp dirs live behind a /var -> /private/var symlink).
    """
    host = (tmp_path / "host").resolve()
    code = host / "code"
    code.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(code)], check=True, capture_output=True, timeout=60)
    backup_dir = code / "libs" / "host_backup"
    backup_dir.mkdir(parents=True)
    (backup_dir / "service.py").write_text("VERSION = 1\n")
    (code / "file.txt").write_text("version 1\n")
    restic_repo = (tmp_path / "restic-repo").resolve()
    env_path = code / "runtime" / "secrets" / "restic.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(f"RESTIC_REPOSITORY={restic_repo}\nRESTIC_PASSWORD={_RESTIC_TEST_PASSWORD}\n")
    run_git_for_backup_test(code, "add", "-A")
    run_git_for_backup_test(code, "commit", "-q", "-m", "initial")
    _restic_for_test(restic_repo, "init")
    return host, code, restic_repo


def _stub_bin_with_restic(
    tmp_path: Path,
    agents_json: str | None = None,
    *,
    sync_ok: bool = True,
    supervisorctl_call_log: Path | None = None,
    supervisorctl_hook: str = "",
    failing_restic_subcommand: str | None = None,
) -> Path:
    """The usual uv/supervisorctl stub dir, plus the real restic on PATH.

    ``failing_restic_subcommand`` swaps the real restic for a wrapper that
    fails exactly that subcommand (e.g. ``restore``) and delegates everything
    else, for injecting failures at a chosen point in the restore script.
    """
    if agents_json is None:
        stub_bin = _make_stub_bin(
            tmp_path,
            sync_ok=sync_ok,
            supervisorctl_call_log=supervisorctl_call_log,
            supervisorctl_hook=supervisorctl_hook,
        )
    else:
        stub_bin = _make_stub_bin(
            tmp_path,
            agents_json=agents_json,
            sync_ok=sync_ok,
            supervisorctl_call_log=supervisorctl_call_log,
            supervisorctl_hook=supervisorctl_hook,
        )
    restic_path = shutil.which(_get_restic_binary())
    assert restic_path is not None, "restic binary not found; run `pnpm build` in apps/minds/"
    restic_entry = stub_bin / "restic"
    if restic_entry.exists() or restic_entry.is_symlink():
        restic_entry.unlink()
    if failing_restic_subcommand is None:
        os.symlink(restic_path, restic_entry)
    else:
        restic_entry.write_text(
            "#!/bin/bash\n"
            f'if [ "$1" = "{failing_restic_subcommand}" ]; then echo "injected restic failure" >&2; exit 1; fi\n'
            f'exec "{restic_path}" "$@"\n'
        )
        restic_entry.chmod(0o755)
    return stub_bin


def _snapshot_entries(restic_repo: Path) -> list[dict]:
    entries = json.loads(_restic_for_test(restic_repo, "snapshots", "--json"))
    assert isinstance(entries, list)
    return entries


def _restore_args(restic_repo: Path, snapshot_id: str, *, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Build the restore script's argv, standing in for what minds resolves and passes.

    In production the snapshot's root and time come from minds' own view of
    the repository (``backup_update._resolve_restore_snapshot``); the script
    only consumes them, so these tests read them straight from restic.
    """
    entry = next(item for item in _snapshot_entries(restic_repo) if item["id"] == snapshot_id)
    return (
        "--agent-id",
        "agent-x",
        "--snapshot-id",
        snapshot_id,
        "--snapshot-root",
        entry["paths"][0],
        "--source-time",
        entry["time"],
    ) + extra


@pytest.mark.timeout(120)
def test_restore_script_rewinds_host_dir_and_takes_a_safety_snapshot(tmp_path: Path) -> None:
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    _restic_for_test(restic_repo, "backup", str(host))
    source_entry = _snapshot_entries(restic_repo)[0]
    snapshot_id = source_entry["id"]
    source_time = source_entry["time"]

    # Work done after the snapshot: a changed file, a new file, and a changed
    # restic.env (the current env must survive the restore; the files must not).
    (code / "file.txt").write_text("version 2\n")
    (code / "extra.txt").write_text("added after the snapshot\n")
    env_path = code / "runtime" / "secrets" / "restic.env"
    current_env = env_path.read_text() + "# current credentials marker\n"
    env_path.write_text(current_env)

    stub_bin = _stub_bin_with_restic(tmp_path)
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "ok", payload
    assert payload["safety_snapshot_taken"] is True
    assert payload["swapped"] is True

    # The host dir is back at the snapshot's content...
    assert (code / "file.txt").read_text() == "version 1\n"
    assert not (code / "extra.txt").exists()
    # ...except the restic.env, which keeps the *current* credentials.
    assert env_path.read_text() == current_env
    # No staging dir left behind.
    assert not (host / ".minds-restore-staging").exists()
    # A pre-restore safety snapshot of the pre-swap state exists (it carries
    # the changed content, so this restore is itself undoable).
    entries = _snapshot_entries(restic_repo)
    safety = [entry for entry in entries if "pre-restore" in (entry.get("tags") or [])]
    assert len(safety) == 1
    # A `restored` snapshot of the swapped-in state was appended, tagged with
    # the source snapshot's time so the UI can label it "Restored from ...".
    assert payload["restored_snapshot_taken"] is True
    restored = [entry for entry in entries if "restored" in (entry.get("tags") or [])]
    assert len(restored) == 1
    assert ("restored-from:" + source_time) in (restored[0].get("tags") or [])
    # It sits on top of the timeline: newer than the pre-restore safety backup.
    assert restored[0]["time"] > safety[0]["time"]


@pytest.mark.timeout(120)
def test_restore_script_skips_a_stale_staging_dir_carried_inside_the_snapshot(tmp_path: Path) -> None:
    # The hourly backup does not exclude the staging dir, so a snapshot taken
    # while an earlier restore was staging carries a stale
    # .minds-restore-staging of its own. The swap must skip it: it is garbage,
    # and moving it onto the live staging dir fails (rename onto a non-empty
    # directory), which used to abort the restore midway.
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    stale_staging = host / ".minds-restore-staging"
    (stale_staging / "leftovers").mkdir(parents=True)
    (stale_staging / "leftovers" / "old.txt").write_text("from an interrupted restore\n")
    _restic_for_test(restic_repo, "backup", str(host))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]
    shutil.rmtree(stale_staging)
    (code / "file.txt").write_text("version 2\n")

    stub_bin = _stub_bin_with_restic(tmp_path)
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "ok", payload
    assert payload["swapped"] is True
    assert (code / "file.txt").read_text() == "version 1\n"
    # Neither the live staging dir nor the snapshot's stale one survives.
    assert not (host / ".minds-restore-staging").exists()


@pytest.mark.timeout(120)
def test_restore_script_descends_into_the_nested_host_dir_of_a_volume_level_snapshot(tmp_path: Path) -> None:
    # On btrfs providers the hourly backup snapshots the whole unified host
    # volume: the snapshot root carries volume-level `agents/` +
    # `host_state.json` next to a `host_dir/` child that holds the actual
    # workspace. The restore must swap in that nested host_dir, not the
    # volume-level entries.
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    volume = (tmp_path / "volume").resolve()
    volume.mkdir()
    shutil.copytree(host, volume / "host_dir")
    (volume / "host_dir" / "code" / "file.txt").write_text("volume snapshot content\n")
    (volume / "agents").mkdir()
    (volume / "host_state.json").write_text("{}\n")
    _restic_for_test(restic_repo, "backup", str(volume))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]

    (code / "file.txt").write_text("current content\n")
    env_path = code / "runtime" / "secrets" / "restic.env"
    current_env = env_path.read_text()

    stub_bin = _stub_bin_with_restic(tmp_path)
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "ok", payload
    assert payload["swapped"] is True
    # The nested host_dir's content landed at the host dir root...
    assert (code / "file.txt").read_text() == "volume snapshot content\n"
    assert env_path.read_text() == current_env
    # ...and the volume-level entries were discarded, not swapped in.
    assert not (host / "host_state.json").exists()
    assert not (host / "host_dir").exists()


@pytest.mark.timeout(120)
def test_restore_script_refuses_a_snapshot_with_no_code_checkout(tmp_path: Path) -> None:
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    junk = (tmp_path / "junk").resolve()
    junk.mkdir()
    (junk / "unrelated.txt").write_text("not a workspace\n")
    _restic_for_test(restic_repo, "backup", str(junk))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]
    (code / "file.txt").write_text("version 2\n")

    stub_bin = _stub_bin_with_restic(tmp_path)
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "failed"
    assert payload["swapped"] is False
    detail = payload["detail"]
    assert isinstance(detail, str)
    assert "no code/ checkout" in detail
    # The host dir was not touched and no staging dir was left behind.
    assert (code / "file.txt").read_text() == "version 2\n"
    assert not (host / ".minds-restore-staging").exists()


@pytest.mark.timeout(120)
def test_restore_script_is_blocked_by_running_chats_and_mutates_nothing(tmp_path: Path) -> None:
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    _restic_for_test(restic_repo, "backup", str(host))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]
    (code / "file.txt").write_text("version 2\n")

    stub_bin = _stub_bin_with_restic(tmp_path, agents_json=_running_chat_agents_json(code))
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "blocked"
    assert payload["running_chats"] == ["chat-1"]
    assert (code / "file.txt").read_text() == "version 2\n"
    # No safety snapshot was taken either -- the gate fires before any mutation.
    assert len(_snapshot_entries(restic_repo)) == 1


# --- restore script: service lifecycle + failure injection ---
#
# Stopping the workspace's services is a side effect that creates a cleanup
# obligation: every exit path afterwards must bring them back. These tests
# force a failure at chosen points (a restic subcommand, uv sync, the swap
# itself) and assert the obligation is discharged. The supervisorctl stub
# records its invocations, so lifecycle ordering is asserted rather than
# assumed.


def _supervisorctl_calls(call_log: Path) -> list[str]:
    if not call_log.exists():
        return []
    return [line.strip() for line in call_log.read_text().splitlines() if line.strip()]


@pytest.mark.timeout(120)
def test_restore_script_stops_all_services_before_the_swap_and_restarts_them_after(tmp_path: Path) -> None:
    # The destructive swap must run writer-free: every supervisord service
    # (not just host-backup) runs from and writes into the host dir, so the
    # script quiesces them all and brings them all back at the end.
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    _restic_for_test(restic_repo, "backup", str(host))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]
    call_log = tmp_path / "supervisorctl-calls.log"

    stub_bin = _stub_bin_with_restic(tmp_path, supervisorctl_call_log=call_log)
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "ok", payload
    assert payload["services_restarted"] is True
    calls = _supervisorctl_calls(call_log)
    assert "stop all" in calls
    assert "restart all" in calls
    assert calls.index("stop all") < calls.index("restart all")
    # The whole workspace is quiesced, not just the backup service.
    assert "stop host-backup" not in calls


@pytest.mark.timeout(120)
def test_restore_script_resumes_services_when_the_restic_restore_fails(tmp_path: Path) -> None:
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    _restic_for_test(restic_repo, "backup", str(host))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]
    (code / "file.txt").write_text("version 2\n")
    call_log = tmp_path / "supervisorctl-calls.log"

    stub_bin = _stub_bin_with_restic(tmp_path, supervisorctl_call_log=call_log, failing_restic_subcommand="restore")
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "failed"
    detail = payload["detail"]
    assert isinstance(detail, str)
    assert "restic restore failed" in detail
    assert payload["safety_snapshot_taken"] is True
    assert payload["swapped"] is False
    # The services came back even though the restore failed.
    assert payload["services_restarted"] is True
    calls = _supervisorctl_calls(call_log)
    assert calls.index("stop all") < calls.index("restart all")
    # Nothing was mutated and no staging dir was left behind.
    assert (code / "file.txt").read_text() == "version 2\n"
    assert not (host / ".minds-restore-staging").exists()


@pytest.mark.timeout(120)
def test_restore_script_resumes_services_when_uv_sync_fails_after_the_swap(tmp_path: Path) -> None:
    # A post-swap failure: the workspace content was already replaced, so the
    # report must say so (swapped=True) and the services must still be
    # brought back for the user to retry from a live workspace.
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    _restic_for_test(restic_repo, "backup", str(host))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]
    (code / "file.txt").write_text("version 2\n")
    call_log = tmp_path / "supervisorctl-calls.log"

    stub_bin = _stub_bin_with_restic(tmp_path, sync_ok=False, supervisorctl_call_log=call_log)
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        _restore_args(restic_repo, snapshot_id),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "failed"
    detail = payload["detail"]
    assert isinstance(detail, str)
    assert "uv sync failed" in detail
    assert payload["swapped"] is True
    assert payload["services_restarted"] is True
    calls = _supervisorctl_calls(call_log)
    assert calls.index("stop all") < calls.index("restart all")
    # The swap itself completed: the host dir carries the snapshot's content.
    assert (code / "file.txt").read_text() == "version 1\n"


@pytest.mark.timeout(120)
@pytest.mark.skipif(os.geteuid() == 0, reason="permission-based swap failure cannot be injected as root")
def test_restore_script_resumes_services_when_the_swap_fails_midway(tmp_path: Path) -> None:
    # The worst failure point: mid-swap, after deletion started. The report
    # must be honest about the workspace being possibly incomplete, and the
    # services must still be restarted (best-effort) rather than left down.
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    _restic_for_test(restic_repo, "backup", str(host))
    snapshot_id = _snapshot_entries(restic_repo)[0]["id"]
    call_log = tmp_path / "supervisorctl-calls.log"
    # An unremovable entry makes the swap's delete pass fail partway: a
    # read-only directory with a child cannot be rmtree'd by a non-root user.
    locked = host / "locked"
    locked.mkdir()
    (locked / "pin.txt").write_text("cannot delete me\n")
    locked.chmod(0o555)

    try:
        stub_bin = _stub_bin_with_restic(tmp_path, supervisorctl_call_log=call_log)
        run = _run_script(
            code,
            BACKUP_RESTORE_SCRIPT,
            _restore_args(restic_repo, snapshot_id),
            extra_path=stub_bin,
            env_overrides={"MNGR_HOST_DIR": str(host)},
        )
        payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
        assert payload is not None, run
        assert payload["status"] == "failed"
        detail = payload["detail"]
        assert isinstance(detail, str)
        assert "swap failed midway" in detail
        assert payload["swapped"] is False
        assert payload["services_restarted"] is True
        assert "restart all" in _supervisorctl_calls(call_log)
    finally:
        # The safety snapshot and staging copies carry the 0o555 mode; restore
        # write permission everywhere so pytest can clean the tmp dir up.
        for dirpath, dirnames, _ in os.walk(host):
            for name in dirnames:
                (Path(dirpath) / name).chmod(0o755)


@pytest.mark.timeout(120)
def test_restore_script_fails_cleanly_without_a_snapshot_root(tmp_path: Path) -> None:
    # minds resolves the snapshot root and passes it in; a dispatch that omits
    # it must fail before anything is stopped or mutated, rather than guessing.
    host, code, restic_repo = _make_restore_workspace(tmp_path)
    _restic_for_test(restic_repo, "backup", str(host))
    (code / "file.txt").write_text("version 2\n")

    stub_bin = _stub_bin_with_restic(tmp_path)
    run = _run_script(
        code,
        BACKUP_RESTORE_SCRIPT,
        ("--agent-id", "agent-x", "--snapshot-id", "ffffffff"),
        extra_path=stub_bin,
        env_overrides={"MNGR_HOST_DIR": str(host)},
    )
    payload = extract_marker_json(run["stdout"], RESTORE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "failed"
    assert payload["safety_snapshot_taken"] is False
    assert "--snapshot-root" in str(payload["detail"])
    # Nothing was mutated.
    assert (code / "file.txt").read_text() == "version 2\n"
