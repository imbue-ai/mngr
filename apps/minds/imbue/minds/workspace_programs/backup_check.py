"""Check a workspace's backup service: its code against the minimum release, its process, and its env.

Verifies the installed backup-service code against the *minimum required*
``minds-v*`` tag (fetching tags from the ``official`` remote only when the tag
is missing locally), reports the supervisord state of the ``host-backup``
program and the current ``data/.secrets/restic.env`` (sha256 + content, so
Imbue Studio can compare against its canonical copy and adopt externally
configured envs). Reads ``--minimum-tag``; answers on one
``CHECK_RESULT_MARKER`` line.
"""

import base64
import hashlib
import os
import sys
from collections.abc import Sequence

from imbue.minds.workspace_programs.backup_protocol import BackupCodeState
from imbue.minds.workspace_programs.backup_protocol import CHECK_RESULT_MARKER
from imbue.minds.workspace_programs.backup_protocol import OFFICIAL_REMOTE_URL
from imbue.minds.workspace_programs.backup_support import RESTIC_ENV_PATH
from imbue.minds.workspace_programs.backup_support import SHORT_COMMAND_TIMEOUT_SECONDS
from imbue.minds.workspace_programs.backup_support import backup_code_path_in_tree
from imbue.minds.workspace_programs.backup_support import fetch_official_tags
from imbue.minds.workspace_programs.backup_support import output_tail
from imbue.minds.workspace_programs.backup_support import resolve_workspace_backup_code_path
from imbue.minds.workspace_programs.backup_support import service_state
from imbue.minds.workspace_programs.backup_support import tag_exists
from imbue.minds.workspace_programs.backup_support import tag_sort_key
from imbue.minds.workspace_programs.program_support import argv_value
from imbue.minds.workspace_programs.program_support import emit_marker_json
from imbue.minds.workspace_programs.program_support import run_command


def _resolve_minimum_tag(minimum_tag: str, official_url: str) -> tuple[str, str]:
    """``(tag, error)``. The minimum tag has no fallback: found or unverifiable."""
    if not minimum_tag:
        return "", "no minimum tag provided"
    if tag_exists(minimum_tag):
        return minimum_tag, ""
    fetch_error = fetch_official_tags(official_url)
    if tag_exists(minimum_tag):
        return minimum_tag, ""
    return "", fetch_error or ("minimum tag %s not found" % minimum_tag)


def _is_cross_layout_content_match(minimum_tag: str, tag_path: str, code_path: str) -> bool:
    """Whether the tag's backup code (at its own layout's path) equals the committed, clean workspace copy.

    Content equality across the renames is tree-hash equality between the tag's
    directory and the committed workspace directory, with a clean worktree at
    that path (the same predicate the same-path worktree diff answers).
    """
    src_tree = run_command(
        ["git", "rev-parse", "-q", "--verify", "%s:%s" % (minimum_tag, tag_path)], SHORT_COMMAND_TIMEOUT_SECONDS
    )
    dest_tree = run_command(
        ["git", "rev-parse", "-q", "--verify", "HEAD:%s" % code_path], SHORT_COMMAND_TIMEOUT_SECONDS
    )
    dirty = run_command(["git", "status", "--porcelain", "--", code_path], SHORT_COMMAND_TIMEOUT_SECONDS)
    return (
        src_tree.returncode == 0
        and dest_tree.returncode == 0
        and src_tree.stdout.strip() == dest_tree.stdout.strip()
        and not dirty.stdout.strip()
    )


def _compare_code_to_tag_content(minimum_tag: str, code_path: str) -> tuple[BackupCodeState, str] | None:
    """``(state, detail)`` when comparing content settles it (matches | unverifiable), else None."""
    tag_path = backup_code_path_in_tree(minimum_tag) or code_path
    if tag_path != code_path:
        # The tag lays the code out at a different layout era's path, so a
        # same-path `git diff` can never match.
        return (
            (BackupCodeState.MATCHES, "") if _is_cross_layout_content_match(minimum_tag, tag_path, code_path) else None
        )
    diffed = run_command(["git", "diff", "--quiet", minimum_tag, "--", code_path], SHORT_COMMAND_TIMEOUT_SECONDS)
    if diffed.returncode == 0:
        return BackupCodeState.MATCHES, ""
    if diffed.returncode != 1:
        return BackupCodeState.UNVERIFIABLE, "git diff failed: %s" % output_tail(diffed, 500)
    return None


def _compute_code_state(minimum_tag: str, installed_version: str, code_path: str) -> tuple[BackupCodeState, str]:
    """``(state, detail)``: matches | newer | outdated | unverifiable.

    At-or-above the minimum is fine. Three ways to establish that:
    1. content matches the minimum tag exactly;
    2. the minimum tag is an ancestor of HEAD (also silently accepts user
       edits on top);
    3. the installed identity (the newest non-reverted `backup-update:` subject,
       else the nearest ancestor minds-v* tag) sorts at or above the minimum --
       needed because backup updates land as *content* commits, which never
       make the minimum tag an ancestor on workspaces created before it.
    """
    content_state = _compare_code_to_tag_content(minimum_tag, code_path)
    if content_state is not None:
        return content_state
    ancestor = run_command(["git", "merge-base", "--is-ancestor", minimum_tag, "HEAD"], SHORT_COMMAND_TIMEOUT_SECONDS)
    if ancestor.returncode == 0:
        return BackupCodeState.NEWER, ""
    if installed_version.startswith("minds-v") and tag_sort_key(installed_version) >= tag_sort_key(minimum_tag):
        return BackupCodeState.NEWER, ""
    return BackupCodeState.OUTDATED, ""


def _installed_backup_version() -> str:
    logged = run_command(["git", "log", "-n", "200", "--format=%s"], SHORT_COMMAND_TIMEOUT_SECONDS)
    # The apply-update program's rollback reverts the update commit, so a
    # `backup-update:` subject with a newer matching revert subject must not
    # count as the installed version.
    pending_reverts: list[str] = []
    for line in logged.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith('Revert "backup-update: ') and stripped.endswith('"'):
            pending_reverts.append(stripped[len('Revert "') : -1])
            continue
        if stripped.startswith("backup-update: "):
            if stripped in pending_reverts:
                pending_reverts.remove(stripped)
                continue
            return stripped[len("backup-update: ") :].strip()
    described = run_command(
        ["git", "describe", "--tags", "--match", "minds-v*", "--abbrev=0"], SHORT_COMMAND_TIMEOUT_SECONDS
    )
    if described.returncode == 0:
        return described.stdout.strip()
    return ""


def _read_restic_env() -> dict[str, object]:
    if not os.path.isfile(RESTIC_ENV_PATH):
        return {"present": False}
    try:
        with open(RESTIC_ENV_PATH, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return {"present": False, "error": str(e)}
    return {
        "present": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_b64": base64.b64encode(data).decode("ascii"),
    }


def _workspace_uptime_seconds() -> float | None:
    # Elapsed seconds of PID 1 = how long this workspace (container/VM) has
    # been up. /proc/uptime is NOT namespaced under plain runc docker (it
    # reports the host's uptime), so PID 1's elapsed time is the portable
    # container-uptime signal.
    listed = run_command(["ps", "-o", "etimes=", "-p", "1"], SHORT_COMMAND_TIMEOUT_SECONDS)
    if listed.returncode != 0:
        return None
    try:
        return float(listed.stdout.strip())
    except ValueError:
        return None


def main(argv: Sequence[str]) -> None:
    minimum = argv_value(argv, "--minimum-tag", "")
    official_url = argv_value(argv, "--official-url", OFFICIAL_REMOTE_URL)
    code_path = resolve_workspace_backup_code_path()
    result: dict[str, object] = {"schema": 1}
    result["uptime_seconds"] = _workspace_uptime_seconds()
    installed_version = _installed_backup_version()
    result["installed_version"] = installed_version
    tag, tag_error = _resolve_minimum_tag(minimum, official_url)
    result["target_tag"] = tag or minimum
    if tag:
        code_state, code_detail = _compute_code_state(tag, installed_version, code_path)
    else:
        code_state, code_detail = BackupCodeState.UNVERIFIABLE, tag_error
    result["code_state"] = code_state.value
    result["code_detail"] = code_detail
    state, state_detail = service_state()
    result["service_state"] = state.value
    result["service_detail"] = state_detail
    result["env"] = _read_restic_env()
    emit_marker_json(CHECK_RESULT_MARKER, result)


if __name__ == "__main__":
    main(sys.argv[1:])
