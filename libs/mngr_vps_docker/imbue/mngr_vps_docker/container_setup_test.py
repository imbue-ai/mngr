import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_vps_docker.container_setup import _build_start_container_script
from imbue.mngr_vps_docker.container_setup import _clone_build_context_for_self_contained_git
from imbue.mngr_vps_docker.container_setup import _remote_sh_command


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo``, returning stdout."""
    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_build_start_container_script_shell_quotes_name() -> None:
    # A hostile name must be shell-quoted so it can't break out of the assignment.
    script = _build_start_container_script("evil; rm -rf /")
    assert "name='evil; rm -rf /'" in script
    assert "__CONTAINER_NAME__" not in script


def test_build_start_container_script_has_recovery_shape() -> None:
    script = _build_start_container_script("my-container")
    # Fast path: a plain docker start.
    assert 'docker start "$name"' in script
    # Recovery only fires on the gVisor self-overlay filestore collision.
    assert "gvisor.filestore" in script
    assert "repeated submounts" in script
    # Reap is scoped to this container id AND runsc (never a broad pattern).
    assert 'grep -F "$cid" | grep runsc' in script
    # Stale on-disk filestore is cleared from the container's overlay dirs.
    assert 'rm -f "$d"/.gvisor.filestore.*' in script


def test_start_container_script_is_valid_posix_sh() -> None:
    # Guard against quoting/syntax regressions in the embedded recovery script.
    script = _build_start_container_script("minds-dev-josh-1-lima-4")
    check = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
    assert check.returncode == 0, check.stderr


def test_remote_sh_command_round_trips() -> None:
    script = _build_start_container_script("c1")
    command = _remote_sh_command(script)
    assert command.endswith("| base64 -d | sh")
    encoded = command.split(" | ", 1)[0].removeprefix("echo ")
    assert base64.b64decode(encoded).decode("utf-8") == script


def test_clone_build_context_returns_none_for_non_git_context(tmp_path: Path) -> None:
    """A non-git context with no --git-depth is uploaded verbatim (no clone)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "Dockerfile").write_text("FROM scratch\n")
    assert _clone_build_context_for_self_contained_git(plain, git_depth=None) is None


@pytest.mark.rsync
def test_clone_build_context_drops_worktree_admin_from_primary_checkout(
    tmp_path: Path, setup_git_config: None
) -> None:
    """A primary checkout with linked worktrees clones to a self-contained .git.

    Regression test for the AWS dogfood template: when ``mngr create`` is run
    from a primary checkout that has per-branch linked worktrees, the raw
    ``.git/worktrees/`` admin would otherwise be baked into the image. There it
    marks the operator's other branches as checked out, which makes the
    post-build mirror seed push fail with "refusing to update checked out
    branch" (``git init --bare`` on the target can't release a branch held by a
    linked worktree). A fresh clone has no linked worktrees at all -- the
    structural property asserted here -- so the seed can update every branch.
    The clone must still carry the operator's uncommitted edits.
    """
    # Primary checkout with two extra branches checked out in linked worktrees
    # (mirrors an operator who keeps a worktree per branch -- the bug repro).
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-b", "main")
    (primary / "f.txt").write_text("base\n")
    _git(primary, "add", "f.txt")
    _git(primary, "commit", "-m", "base")
    for branch in ("mngr/feat-a", "mngr/feat-b"):
        _git(primary, "branch", branch)
        _git(primary, "worktree", "add", str(tmp_path / f"wt-{branch.replace('/', '-')}"), branch)
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
        assert _git(clone, "worktree", "list").strip().count("\n") == 0
        # ...and it still carries the operator's uncommitted edit.
        assert (clone / "dirty.txt").read_text() == "in-flight\n"
    finally:
        # The helper allocates the clone under a fresh tempfile dir; clean it up.
        shutil.rmtree(clone.parent, ignore_errors=True)
