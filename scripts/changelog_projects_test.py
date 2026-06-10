from pathlib import Path

import pytest

from scripts.changelog_projects import DEV_PROJECT
from scripts.changelog_projects import all_known_projects
from scripts.changelog_projects import project_dir
from scripts.changelog_projects import project_entries_dir
from scripts.changelog_projects import project_for_path
from scripts.changelog_projects import pyproject_projects


def _seed_repo(tmp_path: Path) -> Path:
    """Create a minimal repo skeleton with two libs projects and one apps project."""
    (tmp_path / "libs" / "mngr").mkdir(parents=True)
    (tmp_path / "libs" / "mngr" / "pyproject.toml").write_text("")
    (tmp_path / "libs" / "mngr_lima").mkdir(parents=True)
    (tmp_path / "libs" / "mngr_lima" / "pyproject.toml").write_text("")
    (tmp_path / "apps" / "minds").mkdir(parents=True)
    (tmp_path / "apps" / "minds" / "pyproject.toml").write_text("")
    # libs/garbage exists but has no pyproject.toml: not a project.
    (tmp_path / "libs" / "garbage").mkdir(parents=True)
    return tmp_path


def test_project_for_path_libs_match(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_for_path("libs/mngr/imbue/mngr/cli.py", repo) == "mngr"


def test_project_for_path_apps_match(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_for_path("apps/minds/main.py", repo) == "minds"


def test_project_for_path_falls_back_to_dev_for_root_files(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_for_path("scripts/release.py", repo) == DEV_PROJECT
    assert project_for_path("justfile", repo) == DEV_PROJECT
    assert project_for_path(".github/workflows/ci.yml", repo) == DEV_PROJECT


def test_project_for_path_falls_back_to_dev_when_subdir_has_no_pyproject(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_for_path("libs/garbage/foo.py", repo) == DEV_PROJECT


def test_project_for_path_accepts_path_objects(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_for_path(Path("libs/mngr_lima/imbue/mngr_lima/main.py"), repo) == "mngr_lima"


def test_project_dir_libs(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_dir("mngr", repo) == repo / "libs" / "mngr"


def test_project_dir_apps(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_dir("minds", repo) == repo / "apps" / "minds"


def test_project_dir_dev(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_dir(DEV_PROJECT, repo) == repo / DEV_PROJECT


def test_project_dir_raises_on_unknown(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    with pytest.raises(ValueError, match="Unknown project"):
        project_dir("nonexistent", repo)


def test_project_entries_dir_libs(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_entries_dir("mngr", repo) == repo / "libs" / "mngr" / "changelog"


def test_project_entries_dir_apps(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_entries_dir("minds", repo) == repo / "apps" / "minds" / "changelog"


def test_project_entries_dir_dev(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert project_entries_dir(DEV_PROJECT, repo) == repo / DEV_PROJECT / "changelog"


def test_pyproject_projects_excludes_dev(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    # libs/garbage is excluded (no pyproject.toml). dev is not included.
    assert pyproject_projects(repo) == ["minds", "mngr", "mngr_lima"]


def test_all_known_projects_includes_libs_apps_and_dev(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    names = all_known_projects(repo)
    # libs/garbage is excluded (no pyproject.toml).
    # dev is always last.
    assert names == ["minds", "mngr", "mngr_lima", DEV_PROJECT]
