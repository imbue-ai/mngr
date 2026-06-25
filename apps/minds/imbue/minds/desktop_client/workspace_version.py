"""Read a workspace's version history from its own git, from the minds hub.

A workspace is created from the forever-claude-template at a pinned ref (the
immutable ``original_minds_version`` label). Later upgrades are ``git pull``s
from the ``upstream`` remote (the ``update-self`` skill), which land as merge
commits on the workspace's primary branch. So the *current* version and the
*upgrade history* live in the workspace's git, not in any minds-side record.

The hub reads them on demand by running ``git`` inside the (online) workspace
via ``mngr exec``. This is best-effort: an offline workspace, a workspace whose
git has no ``minds-v*`` tags reachable, or any exec failure yields a ``None``
current version and an empty history -- callers fall back to the
``original_minds_version`` label, the one version fact knowable offline.
"""

from datetime import datetime

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.restic_cli import parse_restic_timestamp
from imbue.mngr.primitives import AgentId

# Field separator used in the ``git log`` format string; a tab cannot appear in
# a commit hash or ISO timestamp, and we only keep the subject's first line.
_GIT_LOG_FIELD_SEPARATOR: str = "\t"

# Bounds the in-container git work. Reading refs + a bounded log is fast; a
# generous ceiling still surfaces a wedged exec instead of hanging the route.
_GIT_EXEC_TIMEOUT_SECONDS: float = 30.0

# git commands run inside the workspace. ``describe`` names the nearest
# ``minds-v*`` tag reachable from HEAD (the best-effort "current version");
# the merge log enumerates the upgrades applied since creation.
_GIT_DESCRIBE_ARGS: tuple[str, ...] = ("git", "describe", "--tags", "--match", "minds-v*", "--abbrev=0")
_GIT_MERGES_FORMAT: str = f"%H{_GIT_LOG_FIELD_SEPARATOR}%cI{_GIT_LOG_FIELD_SEPARATOR}%s"
_GIT_MERGES_ARGS: tuple[str, ...] = ("git", "log", "--merges", "--first-parent", f"--format={_GIT_MERGES_FORMAT}")


class UpgradeMerge(FrozenModel):
    """One upgrade merge commit on the workspace's primary branch."""

    commit_sha: str = Field(description="Full commit hash of the merge")
    committed_at: datetime | None = Field(description="Commit time (UTC), if parseable")
    summary: str = Field(description="First line of the merge commit message")


class WorkspaceGitVersion(FrozenModel):
    """Best-effort version facts read from a workspace's own git."""

    current_minds_version: str | None = Field(
        default=None,
        description="Nearest reachable ``minds-v*`` tag (``git describe``), or None when unknown/offline",
    )
    upgrade_merges: tuple[UpgradeMerge, ...] = Field(
        default=(),
        description="Merge commits on the primary branch, newest first (the recorded upgrade history)",
    )


def parse_git_describe(stdout: str) -> str | None:
    """Return the tag named by ``git describe``, or None when there is none."""
    text = stdout.strip()
    return text or None


def parse_upgrade_merges(stdout: str) -> tuple[UpgradeMerge, ...]:
    """Parse the tab-separated ``git log --merges`` output into typed records.

    Each line is ``<sha>\\t<iso-time>\\t<subject>``. Lines that don't carry at
    least a sha and a time field are skipped (defensive against unexpected git
    output); the subject may legitimately be empty.
    """
    merges: list[UpgradeMerge] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(_GIT_LOG_FIELD_SEPARATOR, 2)
        if len(parts) < 2:
            continue
        commit_sha = parts[0].strip()
        if not commit_sha:
            continue
        summary = parts[2] if len(parts) >= 3 else ""
        merges.append(
            UpgradeMerge(
                commit_sha=commit_sha,
                committed_at=parse_restic_timestamp(parts[1]),
                summary=summary,
            )
        )
    return tuple(merges)


def read_workspace_git_version(
    *,
    mngr_binary: str,
    agent_id: AgentId,
    parent_cg: ConcurrencyGroup,
) -> WorkspaceGitVersion:
    """Read current version + upgrade history from a workspace's git via ``mngr exec``.

    Best-effort: any exec or git failure (offline workspace, no tags, etc.) is
    logged at debug and yields the empty/None defaults rather than raising, so
    the version route can always at least report ``original_minds_version``.
    """
    current_version = _exec_git_describe(mngr_binary=mngr_binary, agent_id=agent_id, parent_cg=parent_cg)
    merges = _exec_git_merges(mngr_binary=mngr_binary, agent_id=agent_id, parent_cg=parent_cg)
    return WorkspaceGitVersion(current_minds_version=current_version, upgrade_merges=merges)


def _exec_git_in_workspace(
    *,
    mngr_binary: str,
    agent_id: AgentId,
    git_args: tuple[str, ...],
    parent_cg: ConcurrencyGroup,
) -> str | None:
    """Run a git command inside the workspace via ``mngr exec``; return stdout or None on failure."""
    command = [mngr_binary, "exec", str(agent_id), "--", *git_args]
    cg = parent_cg.make_concurrency_group(name="workspace-git-version")
    try:
        with cg:
            finished = cg.run_process_to_completion(
                command,
                timeout=_GIT_EXEC_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
    except (OSError, ConcurrencyGroupError) as exc:
        logger.debug("Could not exec git in workspace {}: {}", agent_id, exc)
        return None
    if finished.is_timed_out or finished.returncode != 0:
        logger.debug(
            "git {} in workspace {} failed (timed_out={}, rc={})",
            git_args,
            agent_id,
            finished.is_timed_out,
            finished.returncode,
        )
        return None
    return finished.stdout


def _exec_git_describe(*, mngr_binary: str, agent_id: AgentId, parent_cg: ConcurrencyGroup) -> str | None:
    stdout = _exec_git_in_workspace(
        mngr_binary=mngr_binary, agent_id=agent_id, git_args=_GIT_DESCRIBE_ARGS, parent_cg=parent_cg
    )
    return parse_git_describe(stdout) if stdout is not None else None


def _exec_git_merges(*, mngr_binary: str, agent_id: AgentId, parent_cg: ConcurrencyGroup) -> tuple[UpgradeMerge, ...]:
    stdout = _exec_git_in_workspace(
        mngr_binary=mngr_binary, agent_id=agent_id, git_args=_GIT_MERGES_ARGS, parent_cg=parent_cg
    )
    return parse_upgrade_merges(stdout) if stdout is not None else ()
