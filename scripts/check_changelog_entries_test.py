from pathlib import Path

import pytest

from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from scripts.check_changelog_entries import changed_files_against_base
from scripts.check_changelog_entries import find_missing_entries
from scripts.check_changelog_entries import is_exempt_branch
from scripts.check_changelog_entries import main
from scripts.check_changelog_entries import projects_requiring_entry
from scripts.check_changelog_entries import resolve_diff_base


def _init_repo(tmp_path: Path) -> Path:
    """Create a git repo with a project skeleton committed on main.

    Layout mirrors the real repo enough for the project-mapping helpers: a
    ``libs/mngr`` project with a ``pyproject.toml`` and a ``changelog/``
    directory, plus the synthetic ``dev`` bucket directories. Builds on the
    shared ``init_git_repo`` helper (git config isolation, branch ``main``,
    initial commit), then commits the skeleton on top.
    """
    repo = tmp_path / "repo"
    init_git_repo(repo, initial_commit=True)
    (repo / "libs" / "mngr" / "changelog").mkdir(parents=True)
    (repo / "libs" / "mngr" / "pyproject.toml").write_text("")
    (repo / "dev" / "changelog").mkdir(parents=True)
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "project skeleton")
    return repo


@pytest.fixture(autouse=True)
def _clear_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear GitHub Actions env so branch/base detection uses the temp repo's git."""
    for var in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_BASE_REF"):
        monkeypatch.delenv(var, raising=False)


def test_resolve_diff_base_rejects_base_equal_to_head(tmp_path: Path) -> None:
    """The core regression: a repo where main == HEAD (the offload sandbox
    shape) must NOT yield a usable base -- it must raise, not pass."""
    repo = _init_repo(tmp_path)
    # Branch off without adding a commit: HEAD == main, exactly the sandbox case.
    run_git_command(repo, "checkout", "-b", "feature")

    with pytest.raises(RuntimeError, match="distinct from HEAD"):
        resolve_diff_base(repo)


def test_resolve_diff_base_returns_main_when_distinct(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change")

    assert resolve_diff_base(repo) == "main"


def test_changed_files_detects_project_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change")

    changed = changed_files_against_base("main", repo)
    assert "libs/mngr/thing.py" in changed
    assert projects_requiring_entry(changed, repo) == {"mngr"}


def test_root_file_maps_to_dev(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "justfile").write_text("recipe:\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "root change")

    changed = changed_files_against_base("main", repo)
    assert projects_requiring_entry(changed, repo) == {"dev"}


def test_find_missing_entries(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # mngr has no entry for this branch; dev does.
    (repo / "dev" / "changelog" / "feature-x.md").write_text("entry\n")
    missing = find_missing_entries("feature/x", {"mngr", "dev"}, repo)
    assert missing == ["libs/mngr/changelog/feature-x.md"]


def test_main_fails_when_entry_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change")

    assert main(repo) == 1


def test_main_passes_when_entry_present(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    (repo / "libs" / "mngr" / "changelog" / "feature.md").write_text("did a thing\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change with entry")

    assert main(repo) == 0


def test_main_errors_in_sandbox_shape(tmp_path: Path) -> None:
    """End-to-end: the offload-sandbox shape (main == HEAD) returns exit 2, the
    loud-failure code -- never a vacuous 0."""
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")

    assert main(repo) == 2


def test_main_skips_on_main_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # Still on 'main'; nothing to enforce.
    assert main(repo) == 0


def test_is_exempt_branch() -> None:
    assert is_exempt_branch("mngr/changelog-consolidation-2026-06-13")
    assert not is_exempt_branch("mngr/some-feature")
