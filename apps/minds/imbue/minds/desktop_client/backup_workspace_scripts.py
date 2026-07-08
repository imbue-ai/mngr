"""Self-contained python3 scripts minds runs inside a workspace via ``mngr exec``.

Three scripts, all stdlib-only (they run under the workspace's system python3
with no access to minds code), all parameterized via argv and all reporting a
single marker-prefixed JSON line on stdout so the caller can parse a verdict
out of arbitrarily noisy output:

- the *check* script: verifies the installed backup-service code against the
  target ``minds-v*`` tag (fetching tags from ``upstream`` only when the tag
  is missing locally), reports the supervisord state of the ``host-backup``
  program and the current ``runtime/secrets/restic.env`` (sha256 + content,
  so minds can compare against its canonical copy and adopt externally
  configured envs).
- the *gate probe* script: reports which chat agents are actively RUNNING
  (agents sharing the repo-root work_dir, excluding the ``main``-type
  services agent -- worktree/worker agents live elsewhere and never count)
  and whether a backup tick is currently in flight.
- the *apply update* script: the single mutating step -- stash, check out
  ``libs/host_backup`` at the target tag, commit ``backup-update: <tag>``,
  ``uv sync``, restart the service, verify it comes back, and auto-rollback
  (``git revert``) on failure. Optionally stops running chats first.

The scripts are shipped base64-encoded through the shell (the base64 alphabet
contains no shell-significant characters), decoded and piped into ``python3 -``
on the workspace; parameters travel as plain argv.
"""

import base64
import json
import shlex
from typing import Final

CHECK_RESULT_MARKER: Final[str] = "MINDS_BACKUP_CHECK_JSON:"
GATE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_GATE_JSON:"
UPDATE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_UPDATE_JSON:"

# Shared helper functions textually prepended to each script body. Kept as one
# plain string (not f-string) so braces inside the python source need no
# escaping; parameters arrive via sys.argv, never via interpolation.
_SCRIPT_PREAMBLE: Final[str] = r'''
import base64 as _b64
import hashlib as _hashlib
import json as _json
import os as _os
import subprocess as _subprocess
import sys as _sys
import time as _time
import tomllib as _tomllib

BACKUP_CODE_PATH = "libs/host_backup"
RESTIC_ENV_PATH = "runtime/secrets/restic.env"
GIT_IDENTITY = ["-c", "user.name=minds-backup-update", "-c", "user.email=backup-update@minds.local"]
TICK_COMPLETION_TYPES = (
    "RESTIC_BACKUP_SUCCEEDED",
    "RESTIC_BACKUP_FAILED",
    "TICK_SKIPPED_DUE_TO_MISSING_SECRETS",
    "TICK_ERROR",
    "SNAPSHOT_FAILED",
)


def _run(argv, timeout=60):
    try:
        return _subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, _subprocess.TimeoutExpired) as e:
        completed = _subprocess.CompletedProcess(argv, returncode=127)
        completed.stdout = ""
        completed.stderr = "failed to run %s: %s" % (argv[0], e)
        return completed


def _arg_value(flag, default=""):
    argv = _sys.argv[1:]
    for idx, token in enumerate(argv):
        if token == flag and idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def _has_flag(flag):
    return flag in _sys.argv[1:]


def _tag_exists(tag):
    return _run(["git", "rev-parse", "-q", "--verify", "refs/tags/%s" % tag]).returncode == 0


def _ensure_upstream_remote():
    if _run(["git", "remote", "get-url", "upstream"]).returncode == 0:
        return ""
    try:
        with open("parent.toml", "rb") as fh:
            parent = _tomllib.load(fh)
        url = parent.get("url", "")
    except (OSError, ValueError) as e:
        return "cannot read parent.toml: %s" % e
    if not url:
        return "parent.toml has no url"
    added = _run(["git", "remote", "add", "upstream", url])
    if added.returncode != 0:
        return "cannot add upstream remote: %s" % added.stderr.strip()
    return ""


def _fetch_upstream_tags():
    remote_error = _ensure_upstream_remote()
    if remote_error:
        return remote_error
    fetched = _run(["git", "fetch", "upstream", "--tags", "--quiet"], timeout=300)
    if fetched.returncode != 0:
        return "git fetch upstream --tags failed: %s" % (fetched.stderr or fetched.stdout).strip()[-500:]
    return ""


def _tag_sort_key(tag):
    parts = tag[len("minds-v"):].split(".")
    key = []
    for part in parts:
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break
        key.append(int(digits) if digits else 0)
    return key


def _highest_minds_tag():
    listed = _run(["git", "tag", "-l", "minds-v*"])
    tags = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not tags:
        return ""
    return sorted(tags, key=_tag_sort_key)[-1]


def _resolve_target_tag(version):
    """Return (tag, error). Prefers minds-v<version>; falls back to the highest tag."""
    preferred = "minds-v%s" % version if version else ""
    if preferred and _tag_exists(preferred):
        return preferred, ""
    fetch_error = _fetch_upstream_tags()
    if preferred and _tag_exists(preferred):
        return preferred, ""
    best = _highest_minds_tag()
    if best:
        return best, ""
    return "", fetch_error or "no minds-v* tags found"


def _compute_code_state(tag):
    """Return (state, detail): matches | newer | outdated | unverifiable."""
    diffed = _run(["git", "diff", "--quiet", tag, "--", BACKUP_CODE_PATH])
    if diffed.returncode == 0:
        return "matches", ""
    if diffed.returncode != 1:
        return "unverifiable", ("git diff failed: %s" % (diffed.stderr or diffed.stdout).strip()[-500:])
    ancestor = _run(["git", "merge-base", "--is-ancestor", tag, "HEAD"])
    if ancestor.returncode == 0:
        return "newer", ""
    return "outdated", ""


def _installed_backup_version():
    logged = _run(["git", "log", "-n", "200", "--format=%s"])
    for line in logged.stdout.splitlines():
        if line.startswith("backup-update: "):
            return line[len("backup-update: "):].strip()
    described = _run(["git", "describe", "--tags", "--match", "minds-v*", "--abbrev=0"])
    if described.returncode == 0:
        return described.stdout.strip()
    return ""


def _service_state():
    """Return (state, detail): running | not_running | unknown."""
    status = _run(["supervisorctl", "status", "host-backup"])
    text = (status.stdout or "").strip()
    if text and "RUNNING" in text.split():
        return "running", text
    if text:
        return "not_running", text
    return "unknown", (status.stderr or "supervisorctl produced no output").strip()


def _read_restic_env():
    if not _os.path.isfile(RESTIC_ENV_PATH):
        return {"present": False}
    try:
        with open(RESTIC_ENV_PATH, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return {"present": False, "error": str(e)}
    return {
        "present": True,
        "sha256": _hashlib.sha256(data).hexdigest(),
        "content_b64": _b64.b64encode(data).decode("ascii"),
    }


def _list_running_chats():
    """Return (chats, error). Chats = RUNNING agents in this work_dir, excluding type=main."""
    listed = _run(["uv", "run", "mngr", "list", "--format", "json", "--provider", "local"], timeout=180)
    if listed.returncode != 0:
        return None, "mngr list failed: %s" % (listed.stderr or listed.stdout).strip()[-500:]
    payload = None
    for line in reversed(listed.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = _json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "agents" in candidate:
            payload = candidate
            break
    if payload is None:
        return None, "mngr list produced no JSON payload"
    cwd = _os.path.realpath(_os.getcwd())
    chats = []
    for agent in payload.get("agents", []):
        if not isinstance(agent, dict):
            continue
        work_dir = _os.path.realpath(str(agent.get("work_dir", "")))
        if work_dir != cwd:
            continue
        if agent.get("type") == "main":
            continue
        if agent.get("state") != "RUNNING":
            continue
        chats.append(str(agent.get("name") or agent.get("id") or "unknown"))
    return chats, ""


def _backup_events_path(agent_id):
    state_dir = _os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir and agent_id:
        host_dir = _os.environ.get("MNGR_HOST_DIR", "/mngr")
        state_dir = _os.path.join(host_dir, "agents", agent_id)
    if not state_dir:
        return ""
    return _os.path.join(state_dir, "events", "backup", "events.jsonl")


def _is_backup_tick_in_flight(agent_id):
    events_path = _backup_events_path(agent_id)
    if not events_path or not _os.path.isfile(events_path):
        return False
    try:
        with open(events_path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    started = set()
    finished = set()
    for raw in lines[-200:]:
        try:
            event = _json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        tick_id = event.get("tick_id")
        if not isinstance(tick_id, str):
            continue
        event_type = event.get("type")
        if event_type == "BACKUP_STARTED":
            started.add(tick_id)
        elif event_type in TICK_COMPLETION_TYPES:
            finished.add(tick_id)
    return bool(started - finished)


def _emit(marker, payload):
    _sys.stdout.write(marker + _json.dumps(payload) + "\n")
    _sys.stdout.flush()
'''


BACKUP_CHECK_SCRIPT: Final[str] = (
    _SCRIPT_PREAMBLE
    + r"""

def _main():
    version = _arg_value("--minds-version")
    result = {"schema": 1}
    tag, tag_error = _resolve_target_tag(version)
    result["target_tag"] = tag
    if tag:
        code_state, code_detail = _compute_code_state(tag)
    else:
        code_state, code_detail = "unverifiable", tag_error
    result["code_state"] = code_state
    result["code_detail"] = code_detail
    result["installed_version"] = _installed_backup_version()
    service_state, service_detail = _service_state()
    result["service_state"] = service_state
    result["service_detail"] = service_detail
    result["env"] = _read_restic_env()
    _emit("MINDS_BACKUP_CHECK_JSON:", result)


_main()
"""
)


BACKUP_GATE_PROBE_SCRIPT: Final[str] = (
    _SCRIPT_PREAMBLE
    + r"""

def _main():
    agent_id = _arg_value("--agent-id")
    result = {"schema": 1}
    chats, gate_error = _list_running_chats()
    if chats is None:
        result["gate_error"] = gate_error
        result["running_chats"] = []
    else:
        result["running_chats"] = chats
    result["backup_tick_in_flight"] = _is_backup_tick_in_flight(agent_id)
    _emit("MINDS_BACKUP_GATE_JSON:", result)


_main()
"""
)


BACKUP_APPLY_UPDATE_SCRIPT: Final[str] = (
    _SCRIPT_PREAMBLE
    + r"""
# minds already waited (unboundedly, cancellably) for a quiet workspace before
# dispatching this script; this bounded wait only covers a tick that started in
# between, and must stay well inside the caller's outer exec timeout so the
# structured "timed out waiting" payload beats the exec being killed.
_TICK_WAIT_TIMEOUT_SECONDS = 900.0
_TICK_POLL_SECONDS = 5.0
_SERVICE_VERIFY_TIMEOUT_SECONDS = 60.0


def _git(args, timeout=120):
    return _run(["git"] + GIT_IDENTITY + list(args), timeout=timeout)


def _restart_and_verify_service():
    restarted = _run(["supervisorctl", "restart", "host-backup"], timeout=120)
    if restarted.returncode != 0:
        return "supervisorctl restart failed: %s" % (restarted.stderr or restarted.stdout).strip()[-500:]
    deadline = _time.monotonic() + _SERVICE_VERIFY_TIMEOUT_SECONDS
    last_detail = ""
    while _time.monotonic() < deadline:
        state, detail = _service_state()
        last_detail = detail
        if state == "running":
            return ""
        _time.sleep(2.0)
    return "host-backup did not reach RUNNING: %s" % last_detail


def _finish(result, status, detail=""):
    result["status"] = status
    if detail:
        result["detail"] = detail
    _emit("MINDS_BACKUP_UPDATE_JSON:", result)
    _sys.exit(0)


def _pop_stash_into(result):
    if not result.get("stashed"):
        return
    popped = _git(["stash", "pop"])
    if popped.returncode != 0:
        result["stash_conflict"] = True
        result["stash_detail"] = (popped.stderr or popped.stdout).strip()[-500:]


def _main():
    version = _arg_value("--minds-version")
    agent_id = _arg_value("--agent-id")
    is_stop_chats = _has_flag("--stop-chats")
    result = {
        "schema": 1,
        "committed": False,
        "rolled_back": False,
        "stashed": False,
        "stash_conflict": False,
    }

    # Gate: no actively-RUNNING chat agents in this work_dir (optionally stop them).
    chats, gate_error = _list_running_chats()
    if chats is None:
        _finish(result, "failed", "cannot determine running chats: %s" % gate_error)
    if chats and is_stop_chats:
        for chat_name in chats:
            stopped = _run(["uv", "run", "mngr", "stop", chat_name], timeout=180)
            if stopped.returncode != 0:
                _finish(result, "failed", "could not stop chat %s: %s" % (chat_name, (stopped.stderr or stopped.stdout).strip()[-300:]))
        chats, gate_error = _list_running_chats()
        if chats is None:
            _finish(result, "failed", "cannot re-check running chats: %s" % gate_error)
    if chats:
        result["running_chats"] = chats
        _finish(result, "blocked", "chat agents are running")

    # Wait out any in-flight backup tick (minds already waited; this is a
    # bounded belt against a tick starting between its poll and this run).
    wait_deadline = _time.monotonic() + _TICK_WAIT_TIMEOUT_SECONDS
    while _is_backup_tick_in_flight(agent_id):
        if _time.monotonic() >= wait_deadline:
            _finish(result, "failed", "timed out waiting for the in-flight backup tick to finish")
        _time.sleep(_TICK_POLL_SECONDS)

    # Resolve the target tag before touching anything.
    tag, tag_error = _resolve_target_tag(version)
    if not tag:
        _finish(result, "failed", "cannot resolve target tag: %s" % tag_error)
    result["tag"] = tag

    # Stash any uncommitted changes (tracked + untracked) out of the way.
    dirty = _run(["git", "status", "--porcelain"])
    if dirty.stdout.strip():
        stashed = _git(["stash", "push", "--include-untracked", "-m", "minds-backup-update"])
        if stashed.returncode != 0:
            _finish(result, "failed", "git stash failed: %s" % (stashed.stderr or stashed.stdout).strip()[-500:])
        result["stashed"] = True

    # Check out the backup service at the tag; commit only if content changed.
    checked_out = _git(["checkout", tag, "--", BACKUP_CODE_PATH])
    if checked_out.returncode != 0:
        _pop_stash_into(result)
        _finish(result, "failed", "git checkout failed: %s" % (checked_out.stderr or checked_out.stdout).strip()[-500:])
    changed = _run(["git", "status", "--porcelain", "--", BACKUP_CODE_PATH]).stdout.strip()
    committed_sha = ""
    if changed:
        added = _git(["add", BACKUP_CODE_PATH])
        committed = _git(["commit", "-m", "backup-update: %s" % tag])
        if added.returncode != 0 or committed.returncode != 0:
            _git(["checkout", "HEAD", "--", BACKUP_CODE_PATH])
            _pop_stash_into(result)
            _finish(result, "failed", "git commit failed: %s" % (committed.stderr or committed.stdout).strip()[-500:])
        committed_sha = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
        result["committed"] = True

    # Sync dependencies and bounce the service; roll back the commit on failure.
    synced = _run(["uv", "sync"], timeout=900)
    failure_detail = ""
    if synced.returncode != 0:
        failure_detail = "uv sync failed: %s" % (synced.stderr or synced.stdout).strip()[-800:]
    else:
        failure_detail = _restart_and_verify_service()
    if failure_detail:
        if committed_sha:
            reverted = _git(["revert", "--no-edit", committed_sha], timeout=120)
            if reverted.returncode == 0:
                result["rolled_back"] = True
                _run(["uv", "sync"], timeout=900)
                _restart_and_verify_service()
            else:
                failure_detail += "; rollback also failed: %s" % (reverted.stderr or reverted.stdout).strip()[-300:]
        _pop_stash_into(result)
        _finish(result, "failed", failure_detail)

    _pop_stash_into(result)
    _finish(result, "ok")


_main()
"""
)


def build_workspace_script_command(script: str, args: tuple[str, ...]) -> str:
    """Build the shell command string that runs `script` (with argv `args`) on a workspace.

    The script travels base64-encoded (the base64 alphabet contains no
    shell-significant characters, so single-quoting is safe); arguments are
    shell-quoted individually.
    """
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    suffix = f" {quoted_args}" if quoted_args else ""
    return f"printf %s '{encoded}' | base64 -d | python3 -{suffix}"


def extract_marker_json(stdout: str, marker: str) -> dict[str, object] | None:
    """Extract the last marker-prefixed JSON payload from arbitrary stdout, or None."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(marker):
            continue
        try:
            payload = json.loads(stripped[len(marker) :])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
    return None
