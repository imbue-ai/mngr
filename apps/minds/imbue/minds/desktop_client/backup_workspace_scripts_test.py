"""Tests for the workspace-side backup scripts, run for real against temp git repos.

The scripts are executed exactly as they are on a workspace -- through the
base64 shell command via ``bash -c`` -- with a stub ``uv`` / ``supervisorctl``
placed on PATH so the mngr-list gate and the service restart behave like a
healthy (or deliberately broken) workspace without any real infrastructure.
"""

import json
import os
import subprocess
from pathlib import Path

from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_APPLY_UPDATE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_CHECK_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_GATE_PROBE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import CHECK_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import GATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import UPDATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import build_workspace_script_command
from imbue.minds.desktop_client.backup_workspace_scripts import extract_marker_json
from imbue.minds.testing import run_git_for_backup_test
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


def _run_script(repo: Path, script: str, args: tuple[str, ...], *, extra_path: Path | None = None) -> dict:
    command = build_workspace_script_command(script, args)
    env = dict(os.environ)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    env.pop("MNGR_AGENT_STATE_DIR", None)
    result = subprocess.run(
        ["bash", "-c", command], cwd=repo, capture_output=True, text=True, check=False, timeout=300, env=env
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}


def _make_stub_bin(
    tmp_path: Path, *, agents_json: str = '{"agents": [], "errors": []}', restart_ok: bool = True
) -> Path:
    """A PATH dir with stub `uv` and `supervisorctl` acting like a healthy workspace."""
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    uv_stub = stub_bin / "uv"
    uv_stub.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "run" ] && [ "$2" = "mngr" ] && [ "$3" = "list" ]; then\n'
        f"  echo '{agents_json}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)
    write_stub_supervisorctl(stub_bin, is_restart_ok=restart_ok)
    return stub_bin


def _running_chat_agents_json(repo: Path) -> str:
    agents = [
        {"name": "chat-1", "id": "agent-1", "type": "claude", "state": "RUNNING", "work_dir": str(repo)},
        {"name": "services", "id": "agent-2", "type": "main", "state": "RUNNING", "work_dir": str(repo)},
        {"name": "worker-1", "id": "agent-3", "type": "worker", "state": "RUNNING", "work_dir": str(repo / "wt")},
    ]
    return json.dumps({"agents": agents, "errors": []})


# --- marker/command plumbing ---


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
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minds-version", "1.0.0"), extra_path=stub_bin)
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
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minds-version", "1.0.0"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["code_state"] == "newer"


def test_check_script_reports_outdated_when_tag_is_not_an_ancestor(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    # The tag lives on a side branch ahead of main: main's content differs and
    # does not contain the tag -> outdated.
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "release")
    (repo / "libs" / "host_backup" / "service.py").write_text("VERSION = 99\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "newer release content")
    run_git_for_backup_test(repo, "tag", "minds-v2.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minds-version", "2.0.0"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["code_state"] == "outdated"


def test_check_script_falls_back_to_highest_tag_for_unknown_version(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    run_git_for_backup_test(repo, "tag", "minds-v1.2.0")
    (repo / "parent.toml").write_text('url = "file:///nonexistent"\nbranch = "main"\n')
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minds-version", "9.9.9-dev"), extra_path=stub_bin)
    payload = extract_marker_json(run["stdout"], CHECK_RESULT_MARKER)
    assert payload is not None, run
    assert payload["target_tag"] == "minds-v1.2.0"


def test_check_script_reports_env_sha_and_content(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    run_git_for_backup_test(repo, "tag", "minds-v1.0.0")
    env_path = repo / "runtime" / "secrets" / "restic.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n")
    stub_bin = _make_stub_bin(tmp_path)
    run = _run_script(repo, BACKUP_CHECK_SCRIPT, ("--minds-version", "1.0.0"), extra_path=stub_bin)
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


# --- apply update script ---


def test_apply_update_commits_tag_content_and_restores_stash(tmp_path: Path) -> None:
    repo = _make_workspace_repo(tmp_path)
    # The target tag carries newer backup code on a side branch (outdated state).
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "release")
    (repo / "libs" / "host_backup" / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "release content")
    run_git_for_backup_test(repo, "tag", "minds-v2.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")
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
    run_git_for_backup_test(repo, "checkout", "-q", "-b", "release")
    (repo / "libs" / "host_backup" / "service.py").write_text("VERSION = 2\n")
    run_git_for_backup_test(repo, "add", "-A")
    run_git_for_backup_test(repo, "commit", "-q", "-m", "release content")
    run_git_for_backup_test(repo, "tag", "minds-v2.0.0")
    run_git_for_backup_test(repo, "checkout", "-q", "main")
    pre_content = (repo / "libs" / "host_backup" / "service.py").read_text()

    stub_bin = _make_stub_bin(tmp_path, restart_ok=False)
    run = _run_script(
        repo, BACKUP_APPLY_UPDATE_SCRIPT, ("--minds-version", "2.0.0", "--agent-id", "agent-x"), extra_path=stub_bin
    )
    payload = extract_marker_json(run["stdout"], UPDATE_RESULT_MARKER)
    assert payload is not None, run
    assert payload["status"] == "failed"
    assert payload["rolled_back"] is True
    # The revert restored the pre-update content, keeping both commits in history.
    assert (repo / "libs" / "host_backup" / "service.py").read_text() == pre_content
    subjects = run_git_for_backup_test(repo, "log", "--format=%s").splitlines()
    assert subjects[0].startswith('Revert "backup-update: minds-v2.0.0"')
    assert subjects[1] == "backup-update: minds-v2.0.0"


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
