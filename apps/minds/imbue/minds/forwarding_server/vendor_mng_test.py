import subprocess
from pathlib import Path

import pytest

from imbue.minds.errors import VendorError
from imbue.minds.forwarding_server.vendor_mng import MNG_GITHUB_URL
from imbue.minds.forwarding_server.vendor_mng import VENDOR_DIR_NAME
from imbue.minds.forwarding_server.vendor_mng import VENDOR_MNG_DIR_NAME
from imbue.minds.forwarding_server.vendor_mng import _vendor_from_local_repo
from imbue.minds.forwarding_server.vendor_mng import find_mng_repo_root
from imbue.minds.forwarding_server.vendor_mng import vendor_mng
from imbue.minds.testing import add_and_commit_git_repo


def test_find_mng_repo_root_returns_path() -> None:
    """When running from within the mng monorepo, finds the root."""
    root = find_mng_repo_root()
    assert root is not None
    assert (root / "libs" / "mng").is_dir()


def test_vendor_mng_skips_when_vendor_dir_exists(tmp_path: Path) -> None:
    """Skips vendoring if vendor/mng already exists."""
    vendor_dir = tmp_path / VENDOR_DIR_NAME / VENDOR_MNG_DIR_NAME
    vendor_dir.mkdir(parents=True)
    marker = vendor_dir / "marker.txt"
    marker.write_text("existing")

    vendor_mng(tmp_path, mng_repo_root=tmp_path)

    assert marker.read_text() == "existing"


def test_vendor_from_local_repo_clones_repo(vendor_test_repos: tuple[Path, Path], tmp_path: Path) -> None:
    """Clones the source repo into the vendor directory."""
    source, vendor_mng_dir = vendor_test_repos
    (source / "hello.txt").write_text("hello")
    add_and_commit_git_repo(source, tmp_path)

    _vendor_from_local_repo(source, vendor_mng_dir)

    assert (vendor_mng_dir / "hello.txt").read_text() == "hello"


def test_vendor_from_local_repo_applies_uncommitted_changes(
    vendor_test_repos: tuple[Path, Path], tmp_path: Path
) -> None:
    """Uncommitted modifications to tracked files are applied in the vendored copy."""
    source, vendor_mng_dir = vendor_test_repos
    (source / "hello.txt").write_text("original")
    add_and_commit_git_repo(source, tmp_path)

    (source / "hello.txt").write_text("modified")

    _vendor_from_local_repo(source, vendor_mng_dir)

    assert (vendor_mng_dir / "hello.txt").read_text() == "modified"


def test_vendor_from_local_repo_copies_untracked_files(vendor_test_repos: tuple[Path, Path]) -> None:
    """Untracked non-gitignored files are copied to the vendored copy."""
    source, vendor_mng_dir = vendor_test_repos
    (source / "untracked.txt").write_text("untracked content")

    _vendor_from_local_repo(source, vendor_mng_dir)

    assert (vendor_mng_dir / "untracked.txt").read_text() == "untracked content"


def test_vendor_from_local_repo_copies_untracked_files_in_subdirs(vendor_test_repos: tuple[Path, Path]) -> None:
    """Untracked files in subdirectories are copied with their directory structure."""
    source, vendor_mng_dir = vendor_test_repos
    subdir = source / "subdir" / "nested"
    subdir.mkdir(parents=True)
    (subdir / "file.txt").write_text("nested content")

    _vendor_from_local_repo(source, vendor_mng_dir)

    assert (vendor_mng_dir / "subdir" / "nested" / "file.txt").read_text() == "nested content"


def test_vendor_from_local_repo_excludes_gitignored_files(
    vendor_test_repos: tuple[Path, Path], tmp_path: Path
) -> None:
    """Gitignored files are not included in the vendored copy."""
    source, vendor_mng_dir = vendor_test_repos
    (source / ".gitignore").write_text("ignored.txt\n")
    (source / "tracked.txt").write_text("tracked")
    add_and_commit_git_repo(source, tmp_path)

    (source / "ignored.txt").write_text("should be excluded")

    _vendor_from_local_repo(source, vendor_mng_dir)

    assert (vendor_mng_dir / "tracked.txt").read_text() == "tracked"
    assert not (vendor_mng_dir / "ignored.txt").exists()


def test_vendor_from_local_repo_resets_remote_to_github(vendor_test_repos: tuple[Path, Path]) -> None:
    """The git remote origin is reset to point at the GitHub URL."""
    source, vendor_mng_dir = vendor_test_repos

    _vendor_from_local_repo(source, vendor_mng_dir)

    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=vendor_mng_dir,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == MNG_GITHUB_URL


def test_vendor_from_local_repo_raises_on_invalid_source(tmp_path: Path) -> None:
    """Raises VendorError when the source path is not a valid git repo."""
    source = tmp_path / "not-a-repo"
    source.mkdir()

    vendor_mng_dir = tmp_path / "dest" / VENDOR_DIR_NAME / VENDOR_MNG_DIR_NAME
    vendor_mng_dir.parent.mkdir(parents=True)

    with pytest.raises(VendorError, match="Failed to clone local mng repo"):
        _vendor_from_local_repo(source, vendor_mng_dir)


def test_vendor_mng_dev_mode_integration(vendor_test_repos: tuple[Path, Path], tmp_path: Path) -> None:
    """Integration test: vendor_mng with a local mng repo root."""
    source, _ = vendor_test_repos
    (source / "file.txt").write_text("content")
    add_and_commit_git_repo(source, tmp_path)

    mind_dir = tmp_path / "mind"
    mind_dir.mkdir()

    vendor_mng(mind_dir, mng_repo_root=source)

    vendor_mng_dir = mind_dir / VENDOR_DIR_NAME / VENDOR_MNG_DIR_NAME
    assert vendor_mng_dir.exists()
    assert (vendor_mng_dir / "file.txt").read_text() == "content"
