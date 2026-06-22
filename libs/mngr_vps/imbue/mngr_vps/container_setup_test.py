import shutil
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_vps.container_setup import _clone_build_context_for_self_contained_git


def test_clone_build_context_returns_none_for_non_git_context(tmp_path: Path) -> None:
    """A non-git context with no --git-depth is uploaded verbatim (no clone)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "Dockerfile").write_text("FROM scratch\n")
    assert _clone_build_context_for_self_contained_git(plain, git_depth=None) is None


@pytest.mark.rsync
def test_clone_build_context_drops_worktree_admin_from_primary_checkout(temp_git_repo: Path) -> None:
    """A primary checkout with linked worktrees clones to a self-contained .git.

    Regression test for the AWS create-template: when ``mngr create`` is run
    from a primary checkout that has per-branch linked worktrees, the raw
    ``.git/worktrees/`` admin would otherwise be baked into the image. There it
    marks the operator's other branches as checked out, which makes the
    post-build mirror seed push fail with "refusing to update checked out
    branch" (``git init --bare`` on the target can't release a branch held by a
    linked worktree). A fresh clone has no linked worktrees at all -- the
    structural property asserted here -- so the seed can update every branch.
    The clone must still carry the operator's uncommitted edits.
    """
    # temp_git_repo is a primary checkout on `main` with an initial commit. Give
    # it two extra branches checked out in linked worktrees, mirroring an
    # operator who keeps a worktree per branch (the bug repro).
    primary = temp_git_repo
    for branch in ("mngr/feat-a", "mngr/feat-b"):
        run_git_command(primary, "branch", branch)
        run_git_command(primary, "worktree", "add", str(primary.parent / f"wt-{branch.replace('/', '-')}"), branch)
    # An uncommitted edit that must survive into the build context.
    (primary / "dirty.txt").write_text("in-flight\n")
    # Precondition: the raw checkout carries the worktree admin that breaks the seed.
    assert (primary / ".git" / "worktrees").is_dir()

    clone = _clone_build_context_for_self_contained_git(primary, git_depth=None)
    assert clone is not None
    try:
        # The clone is a standalone repo with no linked worktrees, so no branch
        # is held checked-out by a worktree the seed push can't release.
        assert (clone / ".git").is_dir()
        assert not (clone / ".git" / "worktrees").exists()
        assert run_git_command(clone, "worktree", "list").stdout.strip().count("\n") == 0
        # ...and it still carries the operator's uncommitted edit.
        assert (clone / "dirty.txt").read_text() == "in-flight\n"
    finally:
        # The helper allocates the clone under a fresh tempfile dir; clean it up.
        shutil.rmtree(clone.parent, ignore_errors=True)
