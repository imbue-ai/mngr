from pathlib import Path

import pytest

from imbue.minds.errors import ParentTrackingError
from imbue.minds.forwarding_server.parent_tracking import MIND_BRANCH_PREFIX
from imbue.minds.forwarding_server.parent_tracking import PARENT_FILE_NAME
from imbue.minds.forwarding_server.parent_tracking import ParentInfo
from imbue.minds.forwarding_server.parent_tracking import checkout_mind_branch
from imbue.minds.forwarding_server.parent_tracking import commit_parent_file
from imbue.minds.forwarding_server.parent_tracking import fetch_and_merge_parent
from imbue.minds.forwarding_server.parent_tracking import get_current_branch
from imbue.minds.forwarding_server.parent_tracking import get_current_commit_hash
from imbue.minds.forwarding_server.parent_tracking import read_parent_info
from imbue.minds.forwarding_server.parent_tracking import setup_mind_branch_and_parent
from imbue.minds.forwarding_server.parent_tracking import write_parent_info
from imbue.minds.forwarding_server.vendor_mng import run_git
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitCommitHash
from imbue.minds.primitives import GitUrl
from imbue.minds.testing import add_and_commit_git_repo
from imbue.minds.testing import make_git_repo


def test_get_current_branch_returns_branch_name(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    branch = get_current_branch(repo)
    # Default branch is typically "master" or "main" depending on git config
    assert branch in ("master", "main")


def test_get_current_commit_hash_returns_full_hash(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    commit_hash = get_current_commit_hash(repo)
    assert len(commit_hash) == 40
    assert all(c in "0123456789abcdef" for c in commit_hash)


def test_checkout_mind_branch_creates_new_branch(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    checkout_mind_branch(repo, AgentName("selene"))
    branch = get_current_branch(repo)
    assert branch == "{}selene".format(MIND_BRANCH_PREFIX)


def test_write_and_read_parent_info_roundtrips(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    parent = ParentInfo(
        url=GitUrl("https://github.com/org/repo.git"),
        branch=GitBranch("main"),
        hash=GitCommitHash("abc123def456" * 3 + "abcd"),
    )
    write_parent_info(repo, parent)
    result = read_parent_info(repo)
    assert result == parent


def test_read_parent_info_raises_when_file_missing(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    with pytest.raises(ParentTrackingError, match="Failed to read parent.url"):
        read_parent_info(repo)


def test_commit_parent_file_creates_commit(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    parent = ParentInfo(
        url=GitUrl("https://example.com/repo.git"), branch=GitBranch("main"), hash=GitCommitHash("a" * 40)
    )
    write_parent_info(repo, parent)
    commit_parent_file(repo)

    log_output = run_git(
        ["log", "--oneline", "-1"],
        cwd=repo,
        error_message="Failed to read git log",
    )
    assert "parent" in log_output.lower()


def test_setup_mind_branch_and_parent_full_flow(tmp_path: Path) -> None:
    """Verify the full setup flow: branch creation, parent tracking, and commit."""
    source = make_git_repo(tmp_path, "source")
    clone_dir = tmp_path / "clone"

    run_git(["clone", str(source), str(clone_dir)], cwd=tmp_path, error_message="clone failed")

    setup_mind_branch_and_parent(clone_dir, AgentName("selene"), GitUrl(str(source)))

    # Verify branch
    branch = get_current_branch(clone_dir)
    assert branch == "minds/selene"

    # Verify parent info
    parent = read_parent_info(clone_dir)
    assert parent.url == str(source)
    assert parent.branch in ("master", "main")
    assert len(parent.hash) == 40

    # Verify .parent file is committed
    assert (clone_dir / PARENT_FILE_NAME).exists()


def test_fetch_and_merge_parent_merges_changes(tmp_path: Path) -> None:
    """Verify that fetch_and_merge_parent pulls new changes from parent."""
    source = make_git_repo(tmp_path, "source")
    clone_dir = tmp_path / "clone"

    run_git(["clone", str(source), str(clone_dir)], cwd=tmp_path, error_message="clone failed")

    setup_mind_branch_and_parent(clone_dir, AgentName("selene"), GitUrl(str(source)))

    # Make a change in the source repo
    (source / "new_file.txt").write_text("new content")
    add_and_commit_git_repo(source, tmp_path, message="add new file")

    # Read the parent info and merge
    parent_info = read_parent_info(clone_dir)
    new_hash = fetch_and_merge_parent(clone_dir, parent_info)

    # Verify the new file is present in the clone
    assert (clone_dir / "new_file.txt").exists()
    assert (clone_dir / "new_file.txt").read_text() == "new content"

    # Verify the parent hash was updated
    updated_parent = read_parent_info(clone_dir)
    assert updated_parent.hash == new_hash
    assert updated_parent.hash != parent_info.hash


def test_fetch_and_merge_parent_noop_when_up_to_date(tmp_path: Path) -> None:
    """Verify fetch_and_merge_parent succeeds when already up to date."""
    source = make_git_repo(tmp_path, "source")
    clone_dir = tmp_path / "clone"

    run_git(["clone", str(source), str(clone_dir)], cwd=tmp_path, error_message="clone failed")
    setup_mind_branch_and_parent(clone_dir, AgentName("selene"), GitUrl(str(source)))

    parent_info = read_parent_info(clone_dir)
    new_hash = fetch_and_merge_parent(clone_dir, parent_info)

    # Hash should be the same since no changes were made
    assert new_hash == parent_info.hash
