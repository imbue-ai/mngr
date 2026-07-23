"""Self-contained python3 scripts minds runs inside a workspace via ``mngr exec``.

Four scripts, all stdlib-only (they run under the workspace's system python3
with no access to minds code), all parameterized via argv and all reporting a
single marker-prefixed JSON line on stdout so the caller can parse a verdict
out of arbitrarily noisy output:

- the *check* script: verifies the installed backup-service code against the
  *minimum required* ``minds-v*`` tag (fetching tags from the ``official``
  remote only when the tag is missing locally), reports the supervisord state
  of the ``host-backup`` program and the current ``runtime/secrets/restic.env``
  (sha256 + content, so minds can compare against its canonical copy and adopt
  externally configured envs).
- the *gate probe* script: reports which chat agents are actively RUNNING
  (agents sharing the repo-root work_dir, excluding the ``main``-type
  services agent -- worktree/worker agents live elsewhere and never count)
  and whether a backup tick is currently in flight.
- the *apply update* script: the single mutating step -- stash, check out
  ``libs/host_backup`` at the target tag, commit ``backup-update: <tag>``,
  ``uv sync``, restart the service, verify it comes back, and auto-rollback
  (``git revert``) on failure. Optionally stops running chats first.
- the *restore* script: rewinds the whole host dir to a chosen restic
  snapshot, in place -- gate on chats/ticks, stop every supervisord service
  (they all run from and write into the host dir this restore rewrites), take
  a ``pre-restore`` safety snapshot (so any restore is undoable), then
  ``restic restore <id>:<subpath> --target <host_dir> --delete`` with
  ``--overwrite if-changed``: no staging copy, only changed files are
  rewritten, and a failed restore converges when simply re-run. The current
  ``restic.env`` is written back afterwards, a ``restored`` snapshot of the
  restored state is appended (tagged with the source snapshot's time, so the
  timeline shows the restored version as a new "Restored from ..." entry),
  then ``uv sync`` and a restart of every supervisord service. Every exit
  path after the service stop restarts the services (best-effort). The
  snapshot's subpath (the directory inside the snapshot that corresponds to
  the host dir) and timestamp are resolved by minds and passed in via argv,
  so the script never queries restic for metadata minds already holds. The
  in-place restore needs restic >= 0.17; when the workspace's restic is
  older, the script downloads the pinned, sha256-verified build and installs
  it persistently (shadowing the distro binary, so the whole workspace
  converges on the pinned version). Long restic operations stream throttled
  progress lines on stdout so the desktop can show a live accounting.

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
RESTORE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_RESTORE_JSON:"

# The one repository backup-service code is fetched from. minds owns the
# ``official`` remote on every workspace: the scripts create it (or repoint it)
# at this URL, deliberately ignoring ``parent.toml`` -- workspaces created from
# private template clones still receive the official backup code, and the
# ``upstream`` remote name stays reserved for the update-self machinery. Tests
# override it via the scripts' ``--official-url`` argument. Must stay equal to
# the default baked into ``_SCRIPT_PREAMBLE`` (asserted by a unit test).
OFFICIAL_REMOTE_URL: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"

# Shared helper functions textually prepended to each script body. Kept as one
# plain string (not f-string) so braces inside the python source need no
# escaping; parameters arrive via sys.argv, never via interpolation.
_SCRIPT_PREAMBLE: Final[str] = r'''
import base64 as _b64
import hashlib as _hashlib
import json as _json
import os as _os
import shutil as _shutil
import subprocess as _subprocess
import sys as _sys
import time as _time

OFFICIAL_REMOTE_NAME = "official"
DEFAULT_OFFICIAL_REMOTE_URL = "https://github.com/imbue-ai/default-workspace-template.git"
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
# minds already waited (unboundedly, cancellably) for a quiet workspace before
# dispatching a mutating script; this bounded wait only covers a tick that
# started in between, and must stay well inside the caller's outer exec
# timeout so a structured "timed out waiting" payload beats the exec being
# killed.
TICK_WAIT_TIMEOUT_SECONDS = 900.0
TICK_POLL_SECONDS = 5.0
SERVICE_VERIFY_TIMEOUT_SECONDS = 60.0


def _run(argv, timeout=60, cwd=None, env=None):
    try:
        return _subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout, cwd=cwd, env=env
        )
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


def _official_url():
    return _arg_value("--official-url", DEFAULT_OFFICIAL_REMOTE_URL)


def _ensure_official_remote():
    """Idempotently point the `official` remote at the official template URL.

    minds owns this remote name: a missing remote is added and a remote
    pointing anywhere else is repointed, so the fetch below always talks to
    the official repository regardless of what the workspace was created from.
    """
    url = _official_url()
    current = _run(["git", "remote", "get-url", OFFICIAL_REMOTE_NAME])
    if current.returncode == 0:
        if current.stdout.strip() == url:
            return ""
        repointed = _run(["git", "remote", "set-url", OFFICIAL_REMOTE_NAME, url])
        if repointed.returncode != 0:
            return "cannot repoint official remote: %s" % repointed.stderr.strip()
        return ""
    added = _run(["git", "remote", "add", OFFICIAL_REMOTE_NAME, url])
    if added.returncode != 0:
        return "cannot add official remote: %s" % added.stderr.strip()
    return ""


def _fetch_official_tags():
    remote_error = _ensure_official_remote()
    if remote_error:
        return remote_error
    fetched = _run(["git", "fetch", OFFICIAL_REMOTE_NAME, "--tags", "--quiet"], timeout=300)
    if fetched.returncode != 0:
        return "git fetch official --tags failed: %s" % (fetched.stderr or fetched.stdout).strip()[-500:]
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
    fetch_error = _fetch_official_tags()
    if preferred and _tag_exists(preferred):
        return preferred, ""
    best = _highest_minds_tag()
    if best:
        return best, ""
    return "", fetch_error or "no minds-v* tags found"


def _resolve_minimum_tag(minimum_tag):
    """Return (tag, error). The minimum tag has no fallback: found or unverifiable."""
    if not minimum_tag:
        return "", "no minimum tag provided"
    if _tag_exists(minimum_tag):
        return minimum_tag, ""
    fetch_error = _fetch_official_tags()
    if _tag_exists(minimum_tag):
        return minimum_tag, ""
    return "", fetch_error or ("minimum tag %s not found" % minimum_tag)


def _compute_code_state(minimum_tag, installed_version):
    """Return (state, detail): matches | newer | outdated | unverifiable.

    At-or-above the minimum is fine. Three ways to establish that:
    1. content matches the minimum tag exactly;
    2. the minimum tag is an ancestor of HEAD (also silently accepts user
       edits on top);
    3. the installed identity (the newest non-reverted `backup-update:` subject,
       else the nearest ancestor minds-v* tag) sorts at or above the minimum --
       needed because backup updates land as *content* commits, which never
       make the minimum tag an ancestor on workspaces created before it.
    """
    diffed = _run(["git", "diff", "--quiet", minimum_tag, "--", BACKUP_CODE_PATH])
    if diffed.returncode == 0:
        return "matches", ""
    if diffed.returncode != 1:
        return "unverifiable", ("git diff failed: %s" % (diffed.stderr or diffed.stdout).strip()[-500:])
    ancestor = _run(["git", "merge-base", "--is-ancestor", minimum_tag, "HEAD"])
    if ancestor.returncode == 0:
        return "newer", ""
    if installed_version.startswith("minds-v") and _tag_sort_key(installed_version) >= _tag_sort_key(minimum_tag):
        return "newer", ""
    return "outdated", ""


def _installed_backup_version():
    logged = _run(["git", "log", "-n", "200", "--format=%s"])
    # The apply script's rollback reverts the update commit, so a
    # `backup-update:` subject with a newer matching revert subject must not
    # count as the installed version.
    pending_reverts = []
    for line in logged.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith('Revert "backup-update: ') and stripped.endswith('"'):
            pending_reverts.append(stripped[len('Revert "'):-1])
            continue
        if stripped.startswith("backup-update: "):
            if stripped in pending_reverts:
                pending_reverts.remove(stripped)
                continue
            return stripped[len("backup-update: "):].strip()
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
    """Return whether a backup tick is running right now, per the event journal."""
    # Ticks are restic runs owned by the supervised host-backup service; when
    # that service is not RUNNING (or supervisord is unreachable) no tick can
    # be alive, and any started-but-unfinished journal entry is an orphan from
    # a killed tick (e.g. a restore's `stop all`), not live work to wait for.
    service_state, _ = _service_state()
    if service_state != "running":
        return False
    events_path = _backup_events_path(agent_id)
    if not events_path or not _os.path.isfile(events_path):
        return False
    try:
        with open(events_path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    # Ticks run serially in one loop, so only the most recently started tick
    # can be in flight. A tick killed mid-flight (e.g. by a service restart, or
    # by the stop a restore does) never writes its completion event; treating
    # every started-but-unfinished tick as in flight would let one such dead
    # tick block operations for hours, until its line scrolls out of the
    # window. Scoping to the last started tick means the next tick to start
    # supersedes any orphan a restore leaves behind.
    last_started_tick = None
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
            last_started_tick = tick_id
        elif event_type in TICK_COMPLETION_TYPES:
            finished.add(tick_id)
    return last_started_tick is not None and last_started_tick not in finished


def _gate_chats_and_wait_for_tick(agent_id, is_stop_chats, is_chat_gate_skipped=False):
    """Gate a mutating script: no RUNNING chats (optionally stop them), no in-flight tick.

    Returns (status, extra, detail) with status in ok | blocked | failed;
    ``extra`` carries payload fields (e.g. running_chats) for the result.
    ``is_chat_gate_skipped`` bypasses only the chat half (the user explicitly
    forced a restore on a workspace that can no longer answer `mngr list`);
    the tick wait still applies -- it needs no workspace code to answer.
    """
    if not is_chat_gate_skipped:
        chats, gate_error = _list_running_chats()
        if chats is None:
            return "failed", {}, "cannot determine running chats: %s" % gate_error
        if chats and is_stop_chats:
            for chat_name in chats:
                stopped = _run(["uv", "run", "mngr", "stop", chat_name], timeout=180)
                if stopped.returncode != 0:
                    detail = "could not stop chat %s: %s" % (
                        chat_name,
                        (stopped.stderr or stopped.stdout).strip()[-300:],
                    )
                    return "failed", {}, detail
            chats, gate_error = _list_running_chats()
            if chats is None:
                return "failed", {}, "cannot re-check running chats: %s" % gate_error
        if chats:
            return "blocked", {"running_chats": chats}, "chat agents are running"
    wait_deadline = _time.monotonic() + TICK_WAIT_TIMEOUT_SECONDS
    while _is_backup_tick_in_flight(agent_id):
        if _time.monotonic() >= wait_deadline:
            return "failed", {}, "timed out waiting for the in-flight backup tick to finish"
        _time.sleep(TICK_POLL_SECONDS)
    return "ok", {}, ""


def _wait_for_backup_service_running(timeout_seconds=SERVICE_VERIFY_TIMEOUT_SECONDS):
    """Poll until the host-backup program reads RUNNING; return "" or an error detail."""
    deadline = _time.monotonic() + timeout_seconds
    last_detail = ""
    while _time.monotonic() < deadline:
        state, detail = _service_state()
        last_detail = detail
        if state == "running":
            return ""
        _time.sleep(2.0)
    return "host-backup did not reach RUNNING: %s" % last_detail


def _emit(marker, payload):
    _sys.stdout.write(marker + _json.dumps(payload) + "\n")
    _sys.stdout.flush()
'''


BACKUP_CHECK_SCRIPT: Final[str] = (
    _SCRIPT_PREAMBLE
    + r"""

def _main():
    minimum = _arg_value("--minimum-tag")
    result = {"schema": 1}
    installed_version = _installed_backup_version()
    result["installed_version"] = installed_version
    tag, tag_error = _resolve_minimum_tag(minimum)
    result["target_tag"] = tag or minimum
    if tag:
        code_state, code_detail = _compute_code_state(tag, installed_version)
    else:
        code_state, code_detail = "unverifiable", tag_error
    result["code_state"] = code_state
    result["code_detail"] = code_detail
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

def _git(args, timeout=120):
    return _run(["git"] + GIT_IDENTITY + list(args), timeout=timeout)


def _restart_and_verify_service():
    restarted = _run(["supervisorctl", "restart", "host-backup"], timeout=120)
    if restarted.returncode != 0:
        return "supervisorctl restart failed: %s" % (restarted.stderr or restarted.stdout).strip()[-500:]
    return _wait_for_backup_service_running()


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

    # Gate: no actively-RUNNING chat agents in this work_dir (optionally
    # stopping them first), then wait out any in-flight backup tick.
    gate_status, gate_extra, gate_detail = _gate_chats_and_wait_for_tick(agent_id, is_stop_chats)
    result.update(gate_extra)
    if gate_status != "ok":
        _finish(result, gate_status, gate_detail)

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

    # Converge the backup service to exactly the tag's content; commit only if
    # content changed. A plain `git checkout <tag> -- <path>` only overlays
    # paths present in the tag's tree and never deletes files the tag removed,
    # so the tracked content is removed first (the worktree is clean here --
    # everything was stashed above) and then restored from the tag.
    removed = _git(["rm", "-r", "-q", "--ignore-unmatch", "--", BACKUP_CODE_PATH])
    if removed.returncode != 0:
        _git(["checkout", "HEAD", "--", BACKUP_CODE_PATH])
        _pop_stash_into(result)
        _finish(result, "failed", "git rm failed: %s" % (removed.stderr or removed.stdout).strip()[-500:])
    checked_out = _git(["checkout", tag, "--", BACKUP_CODE_PATH])
    if checked_out.returncode != 0:
        _git(["checkout", "HEAD", "--", BACKUP_CODE_PATH])
        _pop_stash_into(result)
        _finish(result, "failed", "git checkout failed: %s" % (checked_out.stderr or checked_out.stdout).strip()[-500:])
    changed = _run(["git", "status", "--porcelain", "--", BACKUP_CODE_PATH]).stdout.strip()
    committed_sha = ""
    if changed:
        added = _git(["add", BACKUP_CODE_PATH])
        if added.returncode != 0:
            _git(["checkout", "HEAD", "--", BACKUP_CODE_PATH])
            _pop_stash_into(result)
            _finish(result, "failed", "git add failed: %s" % (added.stderr or added.stdout).strip()[-500:])
        committed = _git(["commit", "-m", "backup-update: %s" % tag])
        if committed.returncode != 0:
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
                restore_detail = _restart_and_verify_service()
                if restore_detail:
                    failure_detail += "; the rollback commit landed but restoring the service failed: %s" % restore_detail
            else:
                failure_detail += "; rollback also failed: %s" % (reverted.stderr or reverted.stdout).strip()[-300:]
        _pop_stash_into(result)
        _finish(result, "failed", failure_detail)

    _pop_stash_into(result)
    _finish(result, "ok")


_main()
"""
)


# Rewinds the whole host dir (/mngr) to one restic snapshot, in place.
# Parameterized via argv: --agent-id, --snapshot-id, --snapshot-subpath (the
# directory inside the snapshot that corresponds to the host dir, resolved by
# minds from its own view of the repository) and --source-time, plus the
# optional flags --stop-chats, --skip-chat-gate (an explicit user "force
# restore" on a workspace that can no longer answer `mngr list`) and
# --skip-safety-snapshot (an explicit user "restore without backing up first"
# after the safety snapshot failed). Verdict: ok | blocked (running chats) |
# failed.
BACKUP_RESTORE_SCRIPT: Final[str] = (
    _SCRIPT_PREAMBLE
    + r"""
import bz2 as _bz2
import platform as _platform
import select as _select
import tempfile as _tempfile
import tomllib as _tomllib
import urllib.request as _urllib_request

_RESTIC_TIMEOUT_SECONDS = 3000.0
# The in-place restore needs `restic restore --delete` / `--overwrite`,
# which landed in restic 0.17. When the workspace's restic is older (Debian
# bookworm ships 0.14), the pinned build below is downloaded, sha256-verified
# and installed persistently.
_MINIMUM_RESTIC_VERSION = (0, 17, 0)
_PINNED_RESTIC_VERSION = "0.18.1"
_PINNED_RESTIC_SHA256_BY_ARCH = {
    "amd64": "680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d",
    "arm64": "87f53fddde38764095e9c058a3b31834052c37e5826d2acf34e18923c006bd45",
}
_RESTIC_DOWNLOAD_TIMEOUT_SECONDS = 300.0
# Where the downloaded restic lands when /usr/local/bin is not writable. Kept
# out of snapshots (excluded below): it is a regenerable 25MB binary.
_FALLBACK_RESTIC_DIR_NAME = ".minds-restic"
# Forward at most one restic --json status line per interval: restic emits
# them far faster than a human (or the SSE log stream) needs.
_PROGRESS_INTERVAL_SECONDS = 2.0
# Fallback excludes for the safety/restored snapshots when the workspace has
# no readable runtime/backup.toml excludes. Matches host_backup's built-in
# defaults so those snapshots look like every hourly snapshot; when the user
# customized excludes in backup.toml, theirs are used instead (read below).
_DEFAULT_SNAPSHOT_EXCLUDES = (
    "**/.venv",
    "**/node_modules",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/.ruff_cache",
    "**/target",
    "**/dist",
    "**/build",
    "**/.next",
    "**/.cache",
)


# Cleanup obligations of the current run. Every side effect the restore takes
# registers its cleanup here, and _finish pays the debts (best-effort) before
# reporting -- so no individual failure path can forget to bring the services
# back. `is_resume_owed` flips on right after `stop all` and off once the
# success path has run its own verified restart.
_DEBTS = {"is_resume_owed": False}

def _finish(result, status, detail=""):
    if _DEBTS["is_resume_owed"]:
        restarted = _run(["supervisorctl", "restart", "all"], timeout=300)
        result["services_restarted"] = restarted.returncode == 0
        _DEBTS["is_resume_owed"] = False
    result["status"] = status
    if detail:
        result["detail"] = detail
    _emit("MINDS_BACKUP_RESTORE_JSON:", result)
    _sys.exit(0)


def _progress(message):
    # One live progress line; the desktop streams these into the operation log.
    _sys.stdout.write(message + "\n")
    _sys.stdout.flush()


def _parse_env_lines(content):
    env_map = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env_map[key.strip()] = value.strip()
    return env_map


def _restic_environment(env_map):
    env = dict(_os.environ)
    env.update(env_map)
    return env


def _restic_version_tuple(binary):
    # The (major, minor, patch) of `binary`, or None when it is unusable.
    result = _run([binary, "version"], timeout=30)
    if result.returncode != 0:
        return None
    for token in (result.stdout or "").split():
        parts = token.split(".")
        if len(parts) < 2 or not all(part.isdigit() for part in parts):
            continue
        padded = parts + ["0", "0"]
        return (int(padded[0]), int(padded[1]), int(padded[2]))
    return None


def _download_pinned_restic(fallback_path):
    # Download + verify the pinned restic; returns (installed_path, error).
    machine = _platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        return "", "no pinned restic build for architecture %s" % machine
    url = "https://github.com/restic/restic/releases/download/v%s/restic_%s_linux_%s.bz2" % (
        _PINNED_RESTIC_VERSION,
        _PINNED_RESTIC_VERSION,
        arch,
    )
    _progress("Downloading restic %s (%s)..." % (_PINNED_RESTIC_VERSION, arch))
    try:
        with _urllib_request.urlopen(url, timeout=_RESTIC_DOWNLOAD_TIMEOUT_SECONDS) as response:
            compressed = response.read()
    except OSError as e:
        return "", "could not download restic from %s: %s" % (url, e)
    if _hashlib.sha256(compressed).hexdigest() != _PINNED_RESTIC_SHA256_BY_ARCH[arch]:
        return "", "the downloaded restic does not match its pinned sha256; refusing to install it"
    try:
        binary_bytes = _bz2.decompress(compressed)
    except (OSError, ValueError) as e:
        return "", "could not decompress the restic download: %s" % e
    # Persist: prefer /usr/local/bin (shadows the distro binary on PATH, so
    # the whole workspace -- including the hourly host-backup service --
    # converges on the pinned version); fall back to a host-dir location when
    # that is not writable. Written via a temp file + rename so a concurrent
    # reader never sees a half-written binary.
    for destination in ("/usr/local/bin/restic", fallback_path):
        try:
            _os.makedirs(_os.path.dirname(destination), exist_ok=True)
            fd, temp_path = _tempfile.mkstemp(dir=_os.path.dirname(destination))
            with _os.fdopen(fd, "wb") as fh:
                fh.write(binary_bytes)
            _os.chmod(temp_path, 0o755)
            _os.replace(temp_path, destination)
        except OSError:
            continue
        version = _restic_version_tuple(destination)
        if version is not None and version >= _MINIMUM_RESTIC_VERSION:
            _progress("Installed restic %s at %s." % (_PINNED_RESTIC_VERSION, destination))
            return destination, ""
    return "", "could not install the downloaded restic binary"


def _resolve_restic_binary(host_dir, result):
    # Returns (binary, error): any restic at/above the minimum, downloading
    # the pinned one if needed.
    fallback_path = _os.path.join(host_dir, _FALLBACK_RESTIC_DIR_NAME, "restic")
    for candidate in ("restic", fallback_path):
        version = _restic_version_tuple(candidate)
        if version is not None and version >= _MINIMUM_RESTIC_VERSION:
            return candidate, ""
    installed, error = _download_pinned_restic(fallback_path)
    if installed:
        result["restic_downloaded"] = True
    return installed, error


def _read_snapshot_excludes(code_dir):
    # The user's current backup.toml excludes, or host_backup's defaults when
    # absent/unreadable.
    toml_path = _os.path.join(code_dir, "runtime", "backup.toml")
    excludes = list(_DEFAULT_SNAPSHOT_EXCLUDES)
    if _os.path.isfile(toml_path):
        try:
            with open(toml_path, "rb") as fh:
                raw = _tomllib.load(fh)
        except (OSError, ValueError):
            raw = {}
        configured = raw.get("excludes")
        if isinstance(configured, list) and configured and all(isinstance(p, str) for p in configured):
            excludes = list(configured)
    # The downloaded restic fallback binary is regenerable; never snapshot it.
    return excludes + ["**/" + _FALLBACK_RESTIC_DIR_NAME]


def _human_bytes(count):
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f TB" % value


def _format_restic_progress(line):
    # Translates one restic --json output line into a progress line, or None
    # to drop it.
    if not line.startswith("{"):
        return line[:300]
    try:
        payload = _json.loads(line)
    except ValueError:
        return line[:300]
    if not isinstance(payload, dict):
        return None
    message_type = payload.get("message_type")
    if message_type == "status":
        percent = payload.get("percent_done")
        if not isinstance(percent, (int, float)):
            return None
        done = payload.get("bytes_done", payload.get("bytes_restored"))
        total = payload.get("total_bytes")
        detail = ""
        if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total:
            detail = " (%s / %s)" % (_human_bytes(done), _human_bytes(total))
        return "progress: %d%%%s" % (int(percent * 100), detail)
    if message_type == "summary":
        return "progress: 100%"
    if message_type == "error":
        return "restic error: %s" % _json.dumps(payload)[:300]
    return None


def _restic_streaming(args, env_map, restic_binary, timeout=_RESTIC_TIMEOUT_SECONDS):
    # Runs one restic command, streaming throttled progress; returns
    # (returncode, output_tail).
    try:
        process = _subprocess.Popen(
            [restic_binary] + list(args) + ["--json"],
            stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT,
            env=_restic_environment(env_map),
        )
    except OSError as e:
        return 127, "failed to run %s: %s" % (restic_binary, e)
    deadline = _time.monotonic() + timeout
    tail = []
    last_progress_at = 0.0
    fd = process.stdout.fileno()
    buffered = b""

    def _handle_line(raw_line):
        nonlocal last_progress_at
        stripped = raw_line.strip()
        if not stripped:
            return
        tail.append(stripped)
        del tail[:-40]
        message = _format_restic_progress(stripped)
        if message is None:
            return
        if message.startswith("progress:"):
            now = _time.monotonic()
            if now - last_progress_at < _PROGRESS_INTERVAL_SECONDS:
                return
            last_progress_at = now
        _progress(message)

    # Loop until restic closes its output (break) or the deadline passes (the
    # loop condition fails and the while's else reports the timeout).
    while _time.monotonic() < deadline:
        remaining = deadline - _time.monotonic()
        ready, _, _ = _select.select([fd], [], [], max(min(remaining, 5.0), 0.0))
        if not ready:
            continue
        chunk = _os.read(fd, 65536)
        if chunk == b"":
            break
        buffered += chunk
        while b"\n" in buffered:
            raw, buffered = buffered.split(b"\n", 1)
            _handle_line(raw.decode("utf-8", "replace"))
    else:
        process.kill()
        process.wait()
        return 124, "restic %s timed out after %d seconds" % (args[0], int(timeout))
    if buffered:
        _handle_line(buffered.decode("utf-8", "replace"))
    try:
        returncode = process.wait(timeout=60)
    except _subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait()
    return returncode, "\n".join(tail)


def _is_repository_lock_failure(returncode, output):
    # restic >= 0.17 exits 11 on a locked repository; the message match covers
    # subcommand/version variance.
    return returncode == 11 or "repository is already locked" in output or "unable to create lock" in output


def _restic_step_with_unlock_retry(args, env_map, restic_binary):
    # Runs a restic step, clearing a stale repository lock and retrying once
    # if needed. A tick killed by this restore's own `stop all` leaves a lock
    # owned by a dead process; `restic unlock` removes exactly such locks (it
    # never touches a live holder's), so one unlock+retry converges -- the
    # same pattern the host_backup service uses.
    returncode, output = _restic_streaming(args, env_map, restic_binary)
    if returncode == 0 or not _is_repository_lock_failure(returncode, output):
        return returncode, output
    _progress("The repository is locked (likely a backup this restore interrupted); clearing the stale lock...")
    _run([restic_binary, "unlock"], timeout=120, env=_restic_environment(env_map))
    return _restic_streaming(args, env_map, restic_binary)


def _main():
    agent_id = _arg_value("--agent-id")
    snapshot_id = _arg_value("--snapshot-id")
    # Resolved by minds from its own view of the repository and passed in, so
    # this script never queries restic for snapshot metadata.
    snapshot_subpath = _arg_value("--snapshot-subpath")
    source_time = _arg_value("--source-time")
    is_stop_chats = _has_flag("--stop-chats")
    is_chat_gate_skipped = _has_flag("--skip-chat-gate")
    is_safety_snapshot_skipped = _has_flag("--skip-safety-snapshot")
    result = {
        "schema": 2,
        "safety_snapshot_taken": False,
        "safety_snapshot_skipped": is_safety_snapshot_skipped,
        "restored": False,
        "services_restarted": False,
        "restored_snapshot_taken": False,
        "restic_downloaded": False,
    }
    if not snapshot_id:
        _finish(result, "failed", "no --snapshot-id provided")
    if not snapshot_subpath:
        _finish(result, "failed", "no --snapshot-subpath provided")

    # Gate before anything mutates. The chat half needs the current workspace
    # code (`uv run mngr`); a forced restore skips only that half -- the tick
    # wait is self-healing (a stopped host-backup service means no live tick)
    # and needs no workspace code.
    if is_chat_gate_skipped:
        _progress("Skipping the running-chats check (forced restore).")
    gate_status, gate_extra, gate_detail = _gate_chats_and_wait_for_tick(
        agent_id, is_stop_chats, is_chat_gate_skipped=is_chat_gate_skipped
    )
    result.update(gate_extra)
    if gate_status != "ok":
        _finish(result, gate_status, gate_detail)

    code_dir = _os.path.realpath(_os.getcwd())
    host_dir = _os.path.realpath(_os.environ.get("MNGR_HOST_DIR", "/mngr"))
    env_path = _os.path.join(code_dir, RESTIC_ENV_PATH)
    try:
        with open(env_path, "rb") as env_file:
            env_content = env_file.read()
    except OSError as e:
        _finish(result, "failed", "cannot read %s: %s" % (RESTIC_ENV_PATH, e))
    env_map = _parse_env_lines(env_content.decode("utf-8", "replace"))
    if not env_map.get("RESTIC_REPOSITORY"):
        _finish(result, "failed", "%s has no RESTIC_REPOSITORY" % RESTIC_ENV_PATH)

    # Resolve a usable restic (downloading the pinned build if the installed
    # one is too old) and the snapshot excludes before anything is stopped or
    # mutated, so these failures are cheap and leave the workspace untouched.
    restic_binary, restic_error = _resolve_restic_binary(host_dir, result)
    if not restic_binary:
        _finish(result, "failed", restic_error)
    excludes = _read_snapshot_excludes(code_dir)

    # Quiesce the workspace: every supervisord service runs from (and writes
    # into) the host dir this restore rewrites, so a service left running
    # could recreate or hold files mid-restore. Stop them all -- the exec
    # channel this script runs through is not supervisord-managed, so it
    # survives. Best-effort: already-stopped (or missing) services must not
    # abort the restore. From here on, every exit owes a `restart all` --
    # registered as a debt that _finish itself pays, so no failure path can
    # forget it.
    _progress("Stopping the workspace services...")
    _run(["supervisorctl", "stop", "all"], timeout=300)
    _DEBTS["is_resume_owed"] = True

    # Safety snapshot of the current state, so this restore is itself
    # undoable. Skipped only on an explicit user re-dispatch after this very
    # step failed.
    if not is_safety_snapshot_skipped:
        _progress("Backing up the current state (safety snapshot)...")
        backup_args = ["backup", host_dir, "--tag", "pre-restore"]
        for pattern in excludes:
            backup_args += ["--exclude", pattern]
        backed_up, backup_output = _restic_step_with_unlock_retry(backup_args, env_map, restic_binary)
        if backed_up != 0:
            detail = "pre-restore safety snapshot failed: %s" % backup_output[-500:]
            _finish(result, "failed", detail)
        result["safety_snapshot_taken"] = True

    # The in-place sync restore: only files that differ from the snapshot are
    # rewritten, files the snapshot lacks are deleted, and nothing is staged
    # -- so no double disk, and a restore that fails midway converges when
    # simply re-run. The subpath maps the snapshot's recorded layout (volume-
    # level on btrfs providers, the host dir itself on plain docker) onto the
    # host dir; minds resolved and validated it before dispatch.
    _progress("Restoring the selected backup into place...")
    # The restore may rewrite or delete this process's original cwd entries.
    _os.chdir("/")
    restore_args = [
        "restore",
        "%s:%s" % (snapshot_id, snapshot_subpath),
        "--target",
        host_dir,
        "--delete",
        "--overwrite",
        "if-changed",
    ]
    restored, restore_output = _restic_step_with_unlock_retry(restore_args, env_map, restic_binary)
    if restored != 0:
        detail = (
            "the in-place restore failed (%s); the workspace may be mixed between versions -- "
            "running the restore again picks up where this one stopped." % restore_output[-500:]
        )
        _finish(result, "failed", detail)
    if not _os.path.isdir(_os.path.join(host_dir, "code")):
        _finish(result, "failed", "the restore completed but left no code/ checkout at %s" % host_dir)
    result["restored"] = True

    # The snapshot carries whatever restic.env it had at backup time (possibly
    # none); the current credentials must keep working, so write them back.
    try:
        _os.makedirs(_os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "wb") as env_file:
            env_file.write(env_content)
    except OSError as e:
        _finish(result, "failed", "restored, but could not re-write %s: %s" % (RESTIC_ENV_PATH, e))

    # Append a snapshot of the restored state, tagged with its lineage, so the
    # backup timeline shows the restored version as a new entry ("Restored
    # from <source time>") sitting above the pre-restore safety backup. Nearly
    # free: restic dedups against the source snapshot's blobs, so only new
    # metadata is written. Taken now, while the services are still stopped, so
    # nothing is writing to the tree. Best-effort: the restore itself already
    # succeeded and the next backup tick would capture this state anyway, so a
    # failure here must not fail the operation.
    _progress("Recording the restored state in the backup timeline...")
    restored_backup_args = ["backup", host_dir, "--tag", "restored"]
    if source_time:
        restored_backup_args += ["--tag", "restored-from:%s" % source_time]
    for pattern in excludes:
        restored_backup_args += ["--exclude", pattern]
    restored_snapshot, _restored_output = _restic_step_with_unlock_retry(
        restored_backup_args, env_map, restic_binary
    )
    result["restored_snapshot_taken"] = restored_snapshot == 0

    # Backups exclude regenerable trees (.venv etc.), so rebuild dependencies
    # before the services come back.
    _progress("Reinstalling dependencies (uv sync)...")
    synced = _run(["uv", "sync"], timeout=900, cwd=code_dir)
    if synced.returncode != 0:
        detail = "restored, but uv sync failed: %s" % (synced.stderr or synced.stdout).strip()[-800:]
        _finish(result, "failed", detail)

    # Every supervisord service was stopped before the restore; bounce them
    # all onto the restored tree (`restart all` starts stopped programs too).
    # This pays the resume debt directly (and clears it first, so a failed
    # restart is not blindly retried by _finish) because the success path
    # must also verify the service actually came back.
    _progress("Restarting the workspace services...")
    _DEBTS["is_resume_owed"] = False
    restarted = _run(["supervisorctl", "restart", "all"], timeout=300)
    result["services_restarted"] = restarted.returncode == 0
    if restarted.returncode != 0:
        detail = "restored, but restarting services failed: %s" % (restarted.stderr or restarted.stdout).strip()[-500:]
        _finish(result, "failed", detail)
    verify_detail = _wait_for_backup_service_running()
    if verify_detail:
        _finish(result, "failed", "restored, but %s" % verify_detail)
    _finish(result, "ok")


# No catch-all here: an unexpected crash prints its traceback to stderr and
# produces no marker, which the desktop side treats as "script died" -- it
# surfaces the stderr tail and dispatches its own best-effort
# `supervisorctl restart all` (the same backstop that covers an exec timeout,
# which no in-script handler could survive anyway).
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
