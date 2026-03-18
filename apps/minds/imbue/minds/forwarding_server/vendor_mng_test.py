import subprocess
from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import DirtyRepoError
from imbue.minds.errors import VendorError
from imbue.minds.forwarding_server.vendor_mng import VENDOR_DIR_NAME
from imbue.minds.forwarding_server.vendor_mng import check_repo_is_clean
from imbue.minds.forwarding_server.vendor_mng import default_vendor_configs
from imbue.minds.forwarding_server.vendor_mng import find_mng_repo_root
from imbue.minds.forwarding_server.vendor_mng import update_vendor_repos
from imbue.minds.forwarding_server.vendor_mng import vendor_repos
from imbue.minds.testing import add_and_commit_git_repo
from imbue.minds.testing import init_and_commit_git_repo
from imbue.minds.testing import make_git_repo
from imbue.mng_claude_mind.data_types import VendorRepoConfig


def _make_mind_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo to act as the mind directory."""
    mind_dir = tmp_path / "mind"
    mind_dir.mkdir()
    init_and_commit_git_repo(mind_dir, tmp_path, allow_empty=True)
    return mind_dir


def test_find_mng_repo_root_returns_path() -> None:
    """When running from within the mng monorepo, finds the root."""
    root = find_mng_repo_root()
    assert root is not None
    assert (root / "libs" / "mng").is_dir()


def test_vendor_repos_skips_when_vendor_dir_exists(tmp_path: Path) -> None:
    """Skips vendoring if the vendor subdirectory already exists."""
    mind_dir = _make_mind_repo(tmp_path)
    vendor_dir = mind_dir / VENDOR_DIR_NAME / "my-repo"
    vendor_dir.mkdir(parents=True)
    marker = vendor_dir / "marker.txt"
    marker.write_text("existing")

    config = VendorRepoConfig(name=NonEmptyStr("my-repo"), url="https://example.com/repo.git", ref="HEAD")
    vendor_repos(mind_dir, (config,))

    assert marker.read_text() == "existing"


def test_vendor_repos_local_adds_subtree(tmp_path: Path) -> None:
    """A local repo is added as a git subtree under vendor/<name>/."""
    source = make_git_repo(tmp_path, "source")
    mind_dir = _make_mind_repo(tmp_path)

    config = VendorRepoConfig(name=NonEmptyStr("my-lib"), path=str(source))
    vendor_repos(mind_dir, (config,))

    vendor_subdir = mind_dir / VENDOR_DIR_NAME / "my-lib"
    assert vendor_subdir.is_dir()
    assert (vendor_subdir / "hello.txt").read_text() == "hello"


def test_vendor_repos_local_creates_commit(tmp_path: Path) -> None:
    """Subtree addition creates a merge commit in the mind repo."""
    source = make_git_repo(tmp_path, "source")
    mind_dir = _make_mind_repo(tmp_path)

    config = VendorRepoConfig(name=NonEmptyStr("my-lib"), path=str(source))
    vendor_repos(mind_dir, (config,))

    log_output = subprocess.run(
        ["git", "log", "--format=%b"],
        cwd=mind_dir,
        capture_output=True,
        text=True,
    )
    assert "git-subtree-dir: vendor/my-lib" in log_output.stdout


def test_vendor_repos_local_at_specific_ref(tmp_path: Path) -> None:
    """When ref is specified, that exact commit is vendored."""
    source = make_git_repo(tmp_path, "source")

    first_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (source / "hello.txt").write_text("updated")
    add_and_commit_git_repo(source, tmp_path, message="update")

    mind_dir = _make_mind_repo(tmp_path)
    config = VendorRepoConfig(name=NonEmptyStr("my-lib"), path=str(source), ref=first_hash)
    vendor_repos(mind_dir, (config,))

    assert (mind_dir / VENDOR_DIR_NAME / "my-lib" / "hello.txt").read_text() == "hello"


def test_vendor_repos_local_dirty_repo_raises(tmp_path: Path) -> None:
    """Raises DirtyRepoError when the local repo has uncommitted changes."""
    source = make_git_repo(tmp_path, "source")
    (source / "hello.txt").write_text("modified")

    mind_dir = _make_mind_repo(tmp_path)
    config = VendorRepoConfig(name=NonEmptyStr("my-lib"), path=str(source))

    with pytest.raises(DirtyRepoError, match="uncommitted changes"):
        vendor_repos(mind_dir, (config,))


def test_vendor_repos_local_untracked_files_raises(tmp_path: Path) -> None:
    """Raises DirtyRepoError when the local repo has untracked files."""
    source = make_git_repo(tmp_path, "source")
    (source / "new_file.txt").write_text("untracked")

    mind_dir = _make_mind_repo(tmp_path)
    config = VendorRepoConfig(name=NonEmptyStr("my-lib"), path=str(source))

    with pytest.raises(DirtyRepoError, match="uncommitted changes"):
        vendor_repos(mind_dir, (config,))


def test_vendor_repos_multiple_repos(tmp_path: Path) -> None:
    """Multiple repos can be vendored into separate directories."""
    source_a = make_git_repo(tmp_path, "repo-a")
    source_b = tmp_path / "repo-b"
    source_b.mkdir()
    (source_b / "data.txt").write_text("data")
    init_and_commit_git_repo(source_b, tmp_path)

    mind_dir = _make_mind_repo(tmp_path)
    configs = (
        VendorRepoConfig(name=NonEmptyStr("lib-a"), path=str(source_a)),
        VendorRepoConfig(name=NonEmptyStr("lib-b"), path=str(source_b)),
    )
    vendor_repos(mind_dir, configs)

    assert (mind_dir / VENDOR_DIR_NAME / "lib-a" / "hello.txt").read_text() == "hello"
    assert (mind_dir / VENDOR_DIR_NAME / "lib-b" / "data.txt").read_text() == "data"


def test_vendor_repos_invalid_local_path_raises(tmp_path: Path) -> None:
    """Raises VendorError when the local path does not exist."""
    mind_dir = _make_mind_repo(tmp_path)
    config = VendorRepoConfig(name=NonEmptyStr("missing"), path="/nonexistent/path/to/repo")

    with pytest.raises(VendorError, match="does not exist"):
        vendor_repos(mind_dir, (config,))


def test_check_repo_is_clean_passes_for_clean_repo(tmp_path: Path) -> None:
    """check_repo_is_clean does not raise for a clean repo."""
    source = make_git_repo(tmp_path, "source")
    check_repo_is_clean(source)


def test_check_repo_is_clean_raises_for_modified_file(tmp_path: Path) -> None:
    """check_repo_is_clean raises when a tracked file is modified."""
    source = make_git_repo(tmp_path, "source")
    (source / "hello.txt").write_text("changed")

    with pytest.raises(DirtyRepoError):
        check_repo_is_clean(source)


def test_check_repo_is_clean_raises_for_untracked_file(tmp_path: Path) -> None:
    """check_repo_is_clean raises when untracked files exist."""
    source = make_git_repo(tmp_path, "source")
    (source / "new.txt").write_text("new")

    with pytest.raises(DirtyRepoError):
        check_repo_is_clean(source)


def test_default_vendor_configs_dev_mode() -> None:
    """In dev mode, default config uses a local path to the mng repo."""
    mng_root = Path("/fake/mng")
    configs = default_vendor_configs(mng_root)
    assert len(configs) == 1
    assert configs[0].name == "mng"
    assert configs[0].path == str(mng_root)
    assert configs[0].url is None


def test_default_vendor_configs_production_mode() -> None:
    """In production mode, default config uses the GitHub URL."""
    configs = default_vendor_configs(None)
    assert len(configs) == 1
    assert configs[0].name == "mng"
    assert configs[0].url is not None
    assert configs[0].path is None


def test_vendor_repos_remote_adds_subtree(tmp_path: Path) -> None:
    """A remote repo (using a local path as the URL) is added as a subtree."""
    source = make_git_repo(tmp_path, "source")
    mind_dir = _make_mind_repo(tmp_path)

    config = VendorRepoConfig(name=NonEmptyStr("remote-lib"), url=str(source))
    vendor_repos(mind_dir, (config,))

    vendor_subdir = mind_dir / VENDOR_DIR_NAME / "remote-lib"
    assert vendor_subdir.is_dir()
    assert (vendor_subdir / "hello.txt").read_text() == "hello"


# -- update_vendor_repos tests --


def test_update_vendor_repos_pulls_local_changes(tmp_path: Path) -> None:
    """update_vendor_repos pulls new changes from a local source repo."""
    source = make_git_repo(tmp_path, "source")
    mind_dir = _make_mind_repo(tmp_path)

    config = VendorRepoConfig(name=NonEmptyStr("my-lib"), path=str(source))
    vendor_repos(mind_dir, (config,))

    # Make a change in the source repo
    (source / "hello.txt").write_text("updated")
    add_and_commit_git_repo(source, tmp_path, message="update hello")

    # Update the subtree
    update_vendor_repos(mind_dir, (config,))

    assert (mind_dir / VENDOR_DIR_NAME / "my-lib" / "hello.txt").read_text() == "updated"


def test_update_vendor_repos_skips_missing_subtree(tmp_path: Path) -> None:
    """update_vendor_repos skips configs whose vendor directory does not exist."""
    mind_dir = _make_mind_repo(tmp_path)
    source = make_git_repo(tmp_path, "source")

    config = VendorRepoConfig(name=NonEmptyStr("not-vendored"), path=str(source))
    # Should not raise -- just skip the non-existent subtree
    update_vendor_repos(mind_dir, (config,))

    assert not (mind_dir / VENDOR_DIR_NAME / "not-vendored").exists()


def test_update_vendor_repos_remote_pulls_changes(tmp_path: Path) -> None:
    """update_vendor_repos pulls changes from a remote repo (using local path as URL)."""
    source = make_git_repo(tmp_path, "source")
    mind_dir = _make_mind_repo(tmp_path)

    config = VendorRepoConfig(name=NonEmptyStr("remote-lib"), url=str(source))
    vendor_repos(mind_dir, (config,))

    # Make a change in the source repo
    (source / "new_file.txt").write_text("new content")
    add_and_commit_git_repo(source, tmp_path, message="add new file")

    # Update the subtree
    update_vendor_repos(mind_dir, (config,))

    assert (mind_dir / VENDOR_DIR_NAME / "remote-lib" / "new_file.txt").read_text() == "new content"
