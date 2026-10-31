"""Update a workspace's backup service to a release's code: the one mutating step of a backup update.

Stash, check out the backup-service code (``system/services/host_backup``,
``system/libs/host_backup``, or ``libs/host_backup`` on pre-declutter
workspaces) at the target tag, commit ``backup-update: <tag>``,
``uv sync --all-packages``, restart the service, verify it comes back, and
auto-rollback (``git revert``) on failure. With ``--stop-chats`` it stops
running chats first, ending the background commands that would wake them
again. Reads ``--minds-version`` and ``--agent-id``; answers on one
``UPDATE_RESULT_MARKER`` line.
"""

import subprocess
import sys
from collections.abc import Sequence
from typing import Final
from typing import NoReturn

from imbue.minds.workspace_programs.backup_protocol import BackupProgramStatus
from imbue.minds.workspace_programs.backup_protocol import BackupServiceState
from imbue.minds.workspace_programs.backup_protocol import OFFICIAL_REMOTE_URL
from imbue.minds.workspace_programs.backup_protocol import UPDATE_RESULT_MARKER
from imbue.minds.workspace_programs.backup_support import GIT_IDENTITY
from imbue.minds.workspace_programs.backup_support import SERVICE_VERIFY_TIMEOUT_SECONDS
from imbue.minds.workspace_programs.backup_support import SHORT_COMMAND_TIMEOUT_SECONDS
from imbue.minds.workspace_programs.backup_support import backup_code_path_in_tree
from imbue.minds.workspace_programs.backup_support import fetch_official_tags
from imbue.minds.workspace_programs.backup_support import gate_chats_and_wait_for_tick
from imbue.minds.workspace_programs.backup_support import output_tail
from imbue.minds.workspace_programs.backup_support import resolve_workspace_backup_code_path
from imbue.minds.workspace_programs.backup_support import service_state
from imbue.minds.workspace_programs.backup_support import tag_exists
from imbue.minds.workspace_programs.backup_support import tag_sort_key
from imbue.minds.workspace_programs.program_support import argv_value
from imbue.minds.workspace_programs.program_support import emit_marker_json
from imbue.minds.workspace_programs.program_support import has_argv_flag
from imbue.minds.workspace_programs.program_support import poll_until
from imbue.minds.workspace_programs.program_support import run_command

_GIT_TIMEOUT_SECONDS: Final[float] = 120.0
_UV_SYNC_TIMEOUT_SECONDS: Final[float] = 900.0
_SERVICE_RESTART_TIMEOUT_SECONDS: Final[float] = 120.0
_SERVICE_POLL_SECONDS: Final[float] = 2.0


def _git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return run_command(["git", *GIT_IDENTITY, *args], _GIT_TIMEOUT_SECONDS)


def _highest_minds_tag() -> str:
    listed = run_command(["git", "tag", "-l", "minds-v*"], SHORT_COMMAND_TIMEOUT_SECONDS)
    tags = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not tags:
        return ""
    return sorted(tags, key=tag_sort_key)[-1]


def _resolve_target_tag(version: str, official_url: str) -> tuple[str, str]:
    """``(tag, error)``. Prefers minds-v<version>; falls back to the highest tag."""
    preferred = "minds-v%s" % version if version else ""
    if preferred and tag_exists(preferred):
        return preferred, ""
    fetch_error = fetch_official_tags(official_url)
    if preferred and tag_exists(preferred):
        return preferred, ""
    best = _highest_minds_tag()
    if best:
        return best, ""
    return "", fetch_error or "no minds-v* tags found"


def _restart_and_verify_service() -> str:
    """Restart host-backup and wait for it to read RUNNING; "" or an error detail."""
    restarted = run_command(["supervisorctl", "restart", "host-backup"], _SERVICE_RESTART_TIMEOUT_SECONDS)
    if restarted.returncode != 0:
        return "supervisorctl restart failed: %s" % output_tail(restarted, 500)
    state, detail = poll_until(
        service_state,
        lambda sample: sample[0] == BackupServiceState.RUNNING,
        SERVICE_VERIFY_TIMEOUT_SECONDS,
        _SERVICE_POLL_SECONDS,
    )
    if state == BackupServiceState.RUNNING:
        return ""
    return "host-backup did not reach RUNNING: %s" % detail


def _finish(result: dict[str, object], status: BackupProgramStatus, detail: str) -> NoReturn:
    result["status"] = status.value
    if detail:
        result["detail"] = detail
    emit_marker_json(UPDATE_RESULT_MARKER, result)
    sys.exit(0)


def _pop_stash_into(result: dict[str, object]) -> None:
    if not result.get("stashed"):
        return
    popped = _git(["stash", "pop"])
    if popped.returncode != 0:
        result["stash_conflict"] = True
        result["stash_detail"] = output_tail(popped, 500)


def _fail_after_restoring_code(
    result: dict[str, object], code_path: str, step: str, failed: subprocess.CompletedProcess[str]
) -> NoReturn:
    """Put the backup code back as HEAD has it, return the stash, and report ``step`` as failed."""
    _git(["checkout", "HEAD", "--", code_path])
    _pop_stash_into(result)
    _finish(result, BackupProgramStatus.FAILED, "%s failed: %s" % (step, output_tail(failed, 500)))


def _check_out_tag_code(tag: str, code_path: str) -> subprocess.CompletedProcess[str]:
    """Materialize the tag's backup code at the workspace's own path; the last git step's result."""
    tag_path = backup_code_path_in_tree(tag) or code_path
    if tag_path == code_path:
        return _git(["checkout", tag, "--", code_path])
    # The tag stores the code at a different layout era's path; a same-path
    # checkout can never match its tree. Stage the tag's subtree at the
    # workspace's path, then materialize the staged entries.
    read = _git(["read-tree", "--prefix=%s/" % code_path, "%s:%s" % (tag, tag_path)])
    if read.returncode != 0:
        return read
    return _git(["checkout", "--", code_path])


def _converge_code_to_tag(result: dict[str, object], tag: str, code_path: str) -> str:
    """Make the backup code exactly the tag's and commit it when it changed; the commit's sha, or "".

    A plain `git checkout <tag> -- <path>` only overlays paths present in the
    tag's tree and never deletes files the tag removed, so the tracked content
    is removed first (the worktree is clean here -- everything was stashed) and
    then restored from the tag.
    """
    removed = _git(["rm", "-r", "-q", "--ignore-unmatch", "--", code_path])
    if removed.returncode != 0:
        _fail_after_restoring_code(result, code_path, "git rm", removed)
    checked_out = _check_out_tag_code(tag, code_path)
    if checked_out.returncode != 0:
        _fail_after_restoring_code(result, code_path, "git checkout", checked_out)
    changed = run_command(["git", "status", "--porcelain", "--", code_path], SHORT_COMMAND_TIMEOUT_SECONDS)
    if not changed.stdout.strip():
        return ""
    added = _git(["add", code_path])
    if added.returncode != 0:
        _fail_after_restoring_code(result, code_path, "git add", added)
    committed = _git(["commit", "-m", "backup-update: %s" % tag])
    if committed.returncode != 0:
        _fail_after_restoring_code(result, code_path, "git commit", committed)
    result["committed"] = True
    return run_command(["git", "rev-parse", "HEAD"], SHORT_COMMAND_TIMEOUT_SECONDS).stdout.strip()


def _sync_and_restart_service() -> str:
    """Sync dependencies and bounce the service; "" or an error detail.

    --all-packages matters: a root-closure-scoped sync would prune workspace
    members that are not root dependencies from the venv, deleting their
    console scripts and spawn-erroring their supervisord programs.
    """
    synced = run_command(["uv", "sync", "--all-packages"], _UV_SYNC_TIMEOUT_SECONDS)
    if synced.returncode != 0:
        return "uv sync failed: %s" % output_tail(synced, 800)
    return _restart_and_verify_service()


def _roll_back(result: dict[str, object], committed_sha: str, failure_detail: str) -> str:
    """Revert the update commit and bring the previous code's service back; the failure detail, extended."""
    reverted = _git(["revert", "--no-edit", committed_sha])
    if reverted.returncode != 0:
        return failure_detail + "; rollback also failed: %s" % output_tail(reverted, 300)
    result["rolled_back"] = True
    run_command(["uv", "sync", "--all-packages"], _UV_SYNC_TIMEOUT_SECONDS)
    restore_detail = _restart_and_verify_service()
    if restore_detail:
        return failure_detail + "; the rollback commit landed but restoring the service failed: %s" % restore_detail
    return failure_detail


def main(argv: Sequence[str]) -> None:
    version = argv_value(argv, "--minds-version", "")
    agent_id = argv_value(argv, "--agent-id", "")
    is_stop_chats = has_argv_flag(argv, "--stop-chats")
    official_url = argv_value(argv, "--official-url", OFFICIAL_REMOTE_URL)
    code_path = resolve_workspace_backup_code_path()
    result: dict[str, object] = {
        "schema": 1,
        "committed": False,
        "rolled_back": False,
        "stashed": False,
        "stash_conflict": False,
    }

    # Gate: no running or busy chat agents in this work_dir (optionally
    # stopping them first), then wait out any in-flight backup tick.
    gate_status, gate_extra, gate_detail = gate_chats_and_wait_for_tick(
        agent_id, is_stop_chats, is_chat_gate_skipped=False
    )
    result.update(gate_extra)
    if gate_status != BackupProgramStatus.OK:
        _finish(result, gate_status, gate_detail)

    # Resolve the target tag before touching anything.
    tag, tag_error = _resolve_target_tag(version, official_url)
    if not tag:
        _finish(result, BackupProgramStatus.FAILED, "cannot resolve target tag: %s" % tag_error)
    result["tag"] = tag

    # Stash any uncommitted changes (tracked + untracked) out of the way.
    dirty = run_command(["git", "status", "--porcelain"], SHORT_COMMAND_TIMEOUT_SECONDS)
    if dirty.stdout.strip():
        stashed = _git(["stash", "push", "--include-untracked", "-m", "minds-backup-update"])
        if stashed.returncode != 0:
            _finish(result, BackupProgramStatus.FAILED, "git stash failed: %s" % output_tail(stashed, 500))
        result["stashed"] = True

    # Converge the backup service to exactly the tag's content, then bring it
    # up on that code, rolling the commit back when it does not come up.
    committed_sha = _converge_code_to_tag(result, tag, code_path)
    failure_detail = _sync_and_restart_service()
    if failure_detail:
        if committed_sha:
            failure_detail = _roll_back(result, committed_sha, failure_detail)
        _pop_stash_into(result)
        _finish(result, BackupProgramStatus.FAILED, failure_detail)

    _pop_stash_into(result)
    _finish(result, BackupProgramStatus.OK, "")


if __name__ == "__main__":
    main(sys.argv[1:])
