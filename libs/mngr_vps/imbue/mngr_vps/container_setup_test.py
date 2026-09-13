import os
import shutil
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr_vps.container_setup import _clone_build_context_for_self_contained_git
from imbue.mngr_vps.container_setup import _raise_if_cwd_deleted_for_relative_context
from imbue.mngr_vps.container_setup import build_image_on_outer_from_build_args
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


def _delete_own_cwd(doomed: Path) -> None:
    """Chdir into ``doomed`` and unlink it, leaving this process with a dead cwd.

    This is what a create attempt experiences when another attempt removes the
    scratch clone it is running from.
    """
    os.chdir(doomed)
    shutil.rmtree(doomed)


def test_relative_build_context_with_deleted_cwd_raises_legible_error(tmp_path: Path) -> None:
    """The exact failure a create attempt hits when its scratch clone is deleted."""
    doomed = tmp_path / "minds-clone-dwt"
    doomed.mkdir()
    original_cwd = os.getcwd()
    try:
        _delete_own_cwd(doomed)
        # Precondition: the unlinked cwd still stats as present, which is why the
        # caller's is-it-a-path filter waves "." through instead of catching this.
        assert Path(".").exists()
        with pytest.raises(MngrError, match="working directory no longer exists"):
            _raise_if_cwd_deleted_for_relative_context(("--file=system/Dockerfile", "."))
    finally:
        os.chdir(original_cwd)


def test_absolute_build_context_survives_a_deleted_cwd(tmp_path: Path) -> None:
    """An absolute context needs no cwd, so the guard must not block it."""
    doomed = tmp_path / "minds-clone-dwt"
    doomed.mkdir()
    original_cwd = os.getcwd()
    try:
        _delete_own_cwd(doomed)
        _raise_if_cwd_deleted_for_relative_context(("--file=system/Dockerfile", str(tmp_path)))
        # And Path.resolve() on an absolute path really does work without a cwd --
        # the premise that it "calls os.getcwd() even for an absolute path" is false.
        assert Path(str(tmp_path)).resolve() == tmp_path.resolve()
    finally:
        os.chdir(original_cwd)


def test_relative_build_context_with_live_cwd_is_allowed(tmp_path: Path) -> None:
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        _raise_if_cwd_deleted_for_relative_context(("--file=system/Dockerfile", "."))
    finally:
        os.chdir(original_cwd)


def test_build_image_on_outer_checks_the_cwd_before_touching_anything(tmp_path: Path) -> None:
    """The guard is wired into the real entry point, not just defined next to it.

    PR #217 added a correct-looking guard to this function that no input ever
    reached; nothing failed, because nothing called it with a real value. This
    test drives the public function so a future refactor cannot orphan the check.
    ``outer``/``cg``/``builder`` are None: reaching them at all means the guard
    did not fire first.
    """
    doomed = tmp_path / "minds-clone-dwt"
    doomed.mkdir()
    original_cwd = os.getcwd()
    try:
        _delete_own_cwd(doomed)
        with pytest.raises(MngrError, match="working directory no longer exists"):
            build_image_on_outer_from_build_args(
                cast(OuterHostInterface, None),
                cast(Any, None),
                host_id=HostId.generate(),
                docker_build_args=("--file=system/Dockerfile", "."),
                git_depth=None,
                builder=cast(Any, None),
            )
    finally:
        os.chdir(original_cwd)
