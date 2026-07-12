import shutil
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_vps.container_setup import _clone_build_context_for_self_contained_git
from imbue.mngr_vps.container_setup import image_exists


class _ImageInspectOuter(MutableModel):
    """Outer host that succeeds only for ``docker image inspect`` of a known-present image."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    present_image: str

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        is_inspect_of_present = "image inspect" in command and self.present_image in command
        if is_inspect_of_present:
            return CommandResult(stdout="[{}]", stderr="", success=True)
        return CommandResult(stdout="", stderr="No such image", success=False)


def test_image_exists_true_when_inspect_succeeds() -> None:
    outer = cast(OuterHostInterface, _ImageInspectOuter(present_image="default-workspace-template:minds-v9.9.9"))
    assert image_exists(outer, "default-workspace-template:minds-v9.9.9") is True


def test_image_exists_false_when_inspect_fails() -> None:
    outer = cast(OuterHostInterface, _ImageInspectOuter(present_image="default-workspace-template:minds-v9.9.9"))
    assert image_exists(outer, "default-workspace-template:absent-tag") is False


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
