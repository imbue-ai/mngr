import re
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# Branches that never identify work an agent would open a pull request from.
# `HEAD` is what `git rev-parse --abbrev-ref` prints for a detached checkout.
_IGNORED_BRANCH_NAMES: Final[frozenset[str]] = frozenset({"main", "master", "HEAD"})
# The one reflog message shape that records a ref this worktree had checked out.
# Git forbids whitespace in ref names, so both refs parse as non-space runs.
_CHECKOUT_REFLOG_PATTERN: Final[re.Pattern[str]] = re.compile(r"\Acheckout: moving from (\S+) to (\S+)\Z")
_GIT_TIMEOUT_SECONDS: Final[float] = 10.0
_HEAD_COMMAND: Final[tuple[str, ...]] = (
    "git",
    "rev-parse",
    "--abbrev-ref",
    "HEAD",
    "--absolute-git-dir",
    "--git-common-dir",
)
_LOCAL_BRANCHES_COMMAND: Final[tuple[str, ...]] = ("git", "for-each-ref", "--format=%(refname:short)", "refs/heads")

# How many branches one agent contributes, so a worktree that has churned
# through many of them is truncated to its highest-priority ones.
MAX_BRANCHES_PER_AGENT: Final[int] = 8


class WorktreeBranches(FrozenModel):
    """What a worktree's own git metadata says about the branches it has hosted.

    `current_branch` is whatever `git rev-parse --abbrev-ref` printed, including
    the literal `HEAD` of a detached checkout. Which of these are branches worth
    looking up is `select_agent_branches`'s decision.
    """

    current_branch: str = Field(description="Ref name HEAD points at")
    previously_checked_out_branches: tuple[str, ...] = Field(
        description="Local branches the HEAD reflog records this worktree checking out, "
        "most recently checked out first"
    )


class _WorktreeHead(FrozenModel):
    """Where a worktree's HEAD points and where its git metadata lives."""

    current_branch: str = Field(description="Ref name HEAD points at")
    git_dir: Path = Field(description="Absolute path to this worktree's own git directory, which holds its reflog")
    common_dir: Path = Field(
        description="Absolute path to the git directory shared by every worktree of one repository, "
        "which is what identifies the repository whose branch list they all share"
    )


@pure
def select_agent_branches(candidate_refs: Sequence[str | None], max_branch_count: int) -> tuple[str, ...]:
    """Reduce candidate refs, highest priority first, to the branches worth looking up.

    Keeps the caller's order, skips absent refs, drops trunk branches and the
    `HEAD` a detached checkout reports, deduplicates, and keeps at most
    `max_branch_count`.
    """
    selected: list[str] = []
    for ref_name in candidate_refs:
        if len(selected) >= max_branch_count:
            break
        if ref_name is None or ref_name in _IGNORED_BRANCH_NAMES or ref_name in selected:
            continue
        selected.append(ref_name)
    return tuple(selected)


@pure
def parse_reflog_checkout_targets(reflog_text: str) -> tuple[str, ...]:
    """Refs a worktree checked out, most recently checked out first.

    Reads the `checkout: moving from <old> to <new>` messages out of a git
    `logs/HEAD` reflog; each `<new>` is a ref that worktree once had checked
    out. Messages of any other shape (commits, resets, merges) are skipped.

    A target is spelled the way the user typed it, so it can be a branch, a tag,
    an abbreviated commit, or a revision expression like `HEAD~1`. Only the
    repository's ref list tells them apart, which `_read_checkout_targets` does.

    The ref a worktree started on is not among these: `git worktree add -b`
    creates the worktree already on that branch, so nothing ever moved *to*
    it. Callers that know an agent's recorded branch should offer it to
    `select_agent_branches` alongside these.
    """
    targets: list[str] = []
    for line in reversed(reflog_text.splitlines()):
        _, _, message = line.partition("\t")
        match = _CHECKOUT_REFLOG_PATTERN.match(message)
        if match is not None:
            targets.append(match.group(2))
    return tuple(targets)


@pure
def _parse_rev_parse_output(stdout: str, work_dir: Path) -> _WorktreeHead | None:
    """Parse the three lines `git rev-parse` prints, in the order they were asked for.

    `--git-common-dir` answers relative to the work dir in a plain checkout and
    absolutely in a linked worktree, so it is resolved against the work dir --
    joining an absolute path simply discards the work dir.
    """
    lines = stdout.strip().splitlines()
    if len(lines) != 3:
        return None
    return _WorktreeHead(
        current_branch=lines[0].strip(),
        git_dir=Path(lines[1].strip()),
        common_dir=(work_dir / lines[2].strip()).resolve(),
    )


def _read_checkout_targets(git_dir: Path, local_branches: frozenset[str]) -> tuple[str, ...]:
    """Read a worktree's HEAD reflog and return the local branches it checked out.

    A reflog target is spelled the way the user typed it, so `local_branches` is
    what separates a branch from a tag, an abbreviated commit, or a revision
    expression. Anything the repository does not currently carry under
    `refs/heads` is dropped.

    An absent reflog (a repository with `core.logAllRefUpdates` off, or a
    worktree that has never switched refs) yields no targets.

    A reflog holds raw bytes -- committer identities and branch names that git
    constrains to neither ASCII nor UTF-8 -- so undecodable bytes are replaced
    rather than raising. They land in the committer identity, which this parser
    discards; a branch name that carries one just matches no pull request.
    """
    reflog_path = git_dir / "logs" / "HEAD"
    try:
        reflog_text = reflog_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Could not read the HEAD reflog at {}: {}", reflog_path, exc)
        return ()
    return tuple(target for target in parse_reflog_checkout_targets(reflog_text) if target in local_branches)


def _launch_git(cg: ConcurrencyGroup, command: tuple[str, ...], work_dir: Path) -> RunningProcess | None:
    """Start one git command in a work dir, or None if it could not be launched."""
    try:
        return cg.run_process_in_background(
            list(command), cwd=work_dir, timeout=_GIT_TIMEOUT_SECONDS, is_checked_by_group=False
        )
    except (ConcurrencyGroupError, OSError) as exc:
        logger.debug("Failed to launch `{}` in {}: {}", " ".join(command), work_dir, exc)
        return None


def _read_git_stdout(process: RunningProcess, work_dir: Path) -> str | None:
    """Await one git process and return its stdout, or None if it did not succeed."""
    command = " ".join(process.command)
    try:
        process.wait()
    except (ConcurrencyGroupError, TimeoutExpired) as exc:
        logger.debug("`{}` failed in {}: {}", command, work_dir, exc)
        return None
    if process.returncode != 0:
        logger.debug("`{}` exited with code {} in {}", command, process.returncode, work_dir)
        return None
    return process.read_stdout()


def read_worktree_branches(
    work_dirs: Sequence[Path],
    cg: ConcurrencyGroup,
) -> dict[Path, WorktreeBranches]:
    """Read the branch history of each work dir out of its git metadata.

    One `git rev-parse` runs per unique work dir, then one `git for-each-ref`
    per unique *repository* -- every worktree of a repository shares its branch
    list, so a board holding many agents on one repository asks for it once.
    Each round is launched before any of it is awaited, and the reflog itself is
    read straight off disk. Work dirs whose git metadata cannot be read are
    omitted from the result.

    A reflog is an inference, not a record of intent: a branch checked out once
    in passing appears here, and git expires reflog entries (90 days by
    default), so a long-idle branch drops out. Callers are expected to prune
    the result against something authoritative, such as whether a branch
    actually has a pull request.
    """
    heads_by_work_dir = _read_heads(work_dirs, cg)
    local_branches_by_common_dir = _read_local_branches_by_repository(heads_by_work_dir, cg)

    branches_by_work_dir: dict[Path, WorktreeBranches] = {}
    for work_dir, head in heads_by_work_dir.items():
        local_branches = local_branches_by_common_dir.get(head.common_dir)
        if local_branches is None:
            continue
        branches_by_work_dir[work_dir] = WorktreeBranches(
            current_branch=head.current_branch,
            previously_checked_out_branches=_read_checkout_targets(head.git_dir, local_branches),
        )
    return branches_by_work_dir


def _read_heads(work_dirs: Sequence[Path], cg: ConcurrencyGroup) -> dict[Path, _WorktreeHead]:
    """Resolve where each work dir's HEAD points and which git dirs back it."""
    processes: list[tuple[Path, RunningProcess]] = []
    for work_dir in sorted(set(work_dirs)):
        head_process = _launch_git(cg, _HEAD_COMMAND, work_dir)
        if head_process is not None:
            processes.append((work_dir, head_process))

    heads_by_work_dir: dict[Path, _WorktreeHead] = {}
    for work_dir, head_process in processes:
        head_stdout = _read_git_stdout(head_process, work_dir)
        if head_stdout is None:
            continue
        head = _parse_rev_parse_output(head_stdout, work_dir)
        if head is None:
            logger.debug("Unparseable git rev-parse output in {}", work_dir)
            continue
        heads_by_work_dir[work_dir] = head
    return heads_by_work_dir


def _read_local_branches_by_repository(
    heads_by_work_dir: Mapping[Path, _WorktreeHead],
    cg: ConcurrencyGroup,
) -> dict[Path, frozenset[str]]:
    """List the local branches of every repository the given work dirs belong to.

    Keyed by the git dir those worktrees share, so one query covers all of them.
    """
    work_dir_by_common_dir: dict[Path, Path] = {}
    for work_dir, head in heads_by_work_dir.items():
        work_dir_by_common_dir.setdefault(head.common_dir, work_dir)

    processes: list[tuple[Path, Path, RunningProcess]] = []
    for common_dir, work_dir in sorted(work_dir_by_common_dir.items()):
        local_branches_process = _launch_git(cg, _LOCAL_BRANCHES_COMMAND, work_dir)
        if local_branches_process is not None:
            processes.append((common_dir, work_dir, local_branches_process))

    local_branches_by_common_dir: dict[Path, frozenset[str]] = {}
    for common_dir, work_dir, local_branches_process in processes:
        local_branches_stdout = _read_git_stdout(local_branches_process, work_dir)
        if local_branches_stdout is None:
            continue
        # Git forbids whitespace in ref names, so one name per whitespace run.
        local_branches_by_common_dir[common_dir] = frozenset(local_branches_stdout.split())
    return local_branches_by_common_dir
