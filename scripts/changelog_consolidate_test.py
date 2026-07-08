import os
import subprocess
import sys
from pathlib import Path

import pytest

# scripts/changelog_consolidate.py uses bare imports of its sibling modules
# (e.g. `from changelog_projects import ...`), matching how it's invoked
# (`python3 scripts/changelog_consolidate.py`). Make those resolvable for
# pytest by adding scripts/ to sys.path before importing.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from scripts.changelog_consolidate import _build_dated_sections  # noqa: E402
from scripts.changelog_consolidate import _collect_project_entries  # noqa: E402
from scripts.changelog_consolidate import _consolidate_project  # noqa: E402
from scripts.changelog_consolidate import _format_section_line  # noqa: E402
from scripts.changelog_consolidate import _get_entry_added_datetime  # noqa: E402
from scripts.changelog_consolidate import _group_entries_by_date  # noqa: E402
from scripts.changelog_consolidate import _insert_section_into_changelog  # noqa: E402
from scripts.changelog_consolidate import pending_changelog_entries  # noqa: E402


def _seed_libs_project(repo: Path, name: str) -> Path:
    """Create ``libs/<name>/`` with a pyproject.toml and an empty changelog/."""
    project = repo / "libs" / name
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text("")
    (project / "changelog").mkdir(exist_ok=True)
    return project


def test_collect_project_entries_empty_dir(tmp_path: Path) -> None:
    project = _seed_libs_project(tmp_path, "mngr")
    (project / "changelog" / ".gitkeep").touch()
    assert _collect_project_entries(project / "changelog") == []


def test_collect_project_entries_missing_dir_returns_empty(tmp_path: Path) -> None:
    """A project that hasn't been set up yet should not blow up the consolidator."""
    assert _collect_project_entries(tmp_path / "libs" / "nonexistent" / "changelog") == []


def test_collect_project_entries_skips_non_md_files(tmp_path: Path) -> None:
    project = _seed_libs_project(tmp_path, "mngr")
    (project / "changelog" / "notes.txt").write_text("not a changelog entry")
    assert _collect_project_entries(project / "changelog") == []


def test_collect_project_entries_skips_empty_content(tmp_path: Path) -> None:
    project = _seed_libs_project(tmp_path, "mngr")
    (project / "changelog" / "empty.md").write_text("   \n\n  ")
    assert _collect_project_entries(project / "changelog") == []


def test_collect_project_entries_returns_sorted_entries(tmp_path: Path) -> None:
    project = _seed_libs_project(tmp_path, "mngr")
    (project / "changelog" / "b-feature.md").write_text("- Feature B")
    (project / "changelog" / "a-bugfix.md").write_text("- Bugfix A")
    entries = _collect_project_entries(project / "changelog")
    assert len(entries) == 2
    assert entries[0][0].name == "a-bugfix.md"
    assert entries[0][1] == "- Bugfix A"
    assert entries[1][0].name == "b-feature.md"
    assert entries[1][1] == "- Feature B"


def test_pending_changelog_entries_returns_empty_when_no_projects(tmp_path: Path) -> None:
    assert pending_changelog_entries(tmp_path) == []


def test_pending_changelog_entries_walks_every_project(tmp_path: Path) -> None:
    mngr = _seed_libs_project(tmp_path, "mngr")
    (mngr / "changelog" / "notes.txt").write_text("not a changelog entry")
    (mngr / "changelog" / "empty.md").write_text("   \n")
    (mngr / "changelog" / "b.md").write_text("- B")
    (mngr / "changelog" / "a.md").write_text("- A")

    minds = tmp_path / "apps" / "minds"
    minds.mkdir(parents=True)
    (minds / "pyproject.toml").write_text("")
    (minds / "changelog").mkdir()
    (minds / "changelog" / "c.md").write_text("- C")

    # dev/ has no pyproject.toml; it's the synthetic bucket.
    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    (dev_dir / "changelog").mkdir()
    (dev_dir / "changelog" / "d.md").write_text("- D")

    result = pending_changelog_entries(tmp_path)
    # Sorted by project name (alphabetical: 'minds' < 'mngr'), then filename.
    # 'dev' is always last.
    assert [p.name for p in result] == ["c.md", "a.md", "b.md", "d.md"]


def test_format_section_line_joins_dates_newest_first() -> None:
    # The consolidation prompt parses this exact "SECTION <project> <date>..."
    # format, one line per project, dates space-separated newest first.
    assert _format_section_line("mngr", ["2026-06-10", "2026-06-09"]) == "SECTION mngr 2026-06-10 2026-06-09"


def test_format_section_line_single_date() -> None:
    assert _format_section_line("dev", ["2026-06-10"]) == "SECTION dev 2026-06-10"


def test_build_dated_sections_single_date() -> None:
    by_date = {"2026-04-02": [(Path("a.md"), "- Added feature X")]}
    result = _build_dated_sections(by_date)
    assert result == "## 2026-04-02\n\n- Added feature X\n"


def test_build_dated_sections_multiple_dates_newest_first() -> None:
    by_date = {
        "2026-04-01": [(Path("a.md"), "- Feature A")],
        "2026-04-03": [(Path("c.md"), "- Feature C")],
        "2026-04-02": [(Path("b.md"), "- Feature B")],
    }
    result = _build_dated_sections(by_date)
    # Newest date first; sections separated by a blank line
    assert result == "## 2026-04-03\n\n- Feature C\n\n## 2026-04-02\n\n- Feature B\n\n## 2026-04-01\n\n- Feature A\n"


def test_build_dated_sections_multiple_entries_per_date() -> None:
    by_date = {
        "2026-04-02": [
            (Path("a.md"), "- Feature A"),
            (Path("b.md"), "- Feature B"),
        ],
    }
    result = _build_dated_sections(by_date)
    assert result == "## 2026-04-02\n\n- Feature A\n\n- Feature B\n"


def test_insert_section_errors_when_file_missing(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    with pytest.raises(FileNotFoundError):
        _insert_section_into_changelog(changelog_path, "## 2026-04-02\n\n- Entry\n")


def test_insert_section_first_consolidation(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text("# Changelog\n\nDescription text.\n")
    _insert_section_into_changelog(changelog_path, "## 2026-04-02\n\n- Entry\n")
    result = changelog_path.read_text()
    assert "# Changelog\n\nDescription text.\n\n## 2026-04-02\n\n- Entry\n" == result


def test_insert_section_before_existing_section(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text("# Changelog\n\nDescription.\n\n## 2026-04-01\n\n- Old entry\n")
    _insert_section_into_changelog(changelog_path, "## 2026-04-02\n\n- New entry\n")
    result = changelog_path.read_text()
    # New section should appear before old section, with a blank line between them
    assert "- New entry\n\n## 2026-04-01" in result
    # New section should appear after the description
    assert result.index("## 2026-04-02") < result.index("## 2026-04-01")


def test_insert_section_no_blank_line_after_header(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text("# Changelog\n## 2026-04-01\n\n- Old entry\n")
    _insert_section_into_changelog(changelog_path, "## 2026-04-02\n\n- New entry\n")
    result = changelog_path.read_text()
    # New section should appear after the header and before the old section
    assert result.index("# Changelog") < result.index("## 2026-04-02") < result.index("## 2026-04-01")


def test_insert_section_preserves_multiple_existing_sections(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text(
        "# Changelog\n\nDescription.\n\n## 2026-04-01\n\n- Entry 1\n\n## 2026-03-31\n\n- Entry 0\n"
    )
    _insert_section_into_changelog(changelog_path, "## 2026-04-02\n\n- Entry 2\n")
    result = changelog_path.read_text()
    # All three sections should be present in order
    idx_new = result.index("## 2026-04-02")
    idx_mid = result.index("## 2026-04-01")
    idx_old = result.index("## 2026-03-31")
    assert idx_new < idx_mid < idx_old


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }


def _init_git_repo_with_files(repo: Path, files_with_dates: list[tuple[str, str, str]]) -> None:
    """Init a temp git repo and commit each file at its given committer date.

    files_with_dates: list of (rel_path, content, iso_date) tuples. Each entry
    is added in its own commit on the (linear) main branch.
    """
    env = _git_env()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    for rel_path, content, iso_date in files_with_dates:
        path = repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        subprocess.run(["git", "add", rel_path], cwd=repo, check=True, env=env)
        commit_env = {**env, "GIT_AUTHOR_DATE": iso_date, "GIT_COMMITTER_DATE": iso_date}
        subprocess.run(
            ["git", "commit", "-q", "-m", f"add {rel_path}"],
            cwd=repo,
            check=True,
            env=commit_env,
        )


def test_get_entry_added_datetime_uses_committer_date(tmp_path: Path) -> None:
    """The helper returns the committer date of the commit that added the file, in PT."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(
        repo,
        [
            # 2026-05-08T18:00:00Z = 2026-05-08T11:00:00 PT (PDT, UTC-7)
            ("libs/mngr/changelog/foo.md", "- entry foo\n", "2026-05-08T18:00:00Z"),
        ],
    )
    dt = _get_entry_added_datetime(repo / "libs" / "mngr" / "changelog" / "foo.md", repo)
    assert dt.strftime("%Y-%m-%d") == "2026-05-08"
    assert dt.strftime("%H") == "11"


def test_get_entry_added_datetime_returns_merge_commit_date(tmp_path: Path) -> None:
    """For files merged in via a merge commit, return the merge commit's date,
    not the feature-branch commit's date.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("# repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, env=env)
    seed_env = {**env, "GIT_AUTHOR_DATE": "2026-05-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-05-01T00:00:00Z"}
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, env=seed_env)

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True, env=env)
    (repo / "libs" / "mngr" / "changelog").mkdir(parents=True)
    (repo / "libs" / "mngr" / "changelog" / "foo.md").write_text("- entry foo\n")
    subprocess.run(["git", "add", "libs/mngr/changelog/foo.md"], cwd=repo, check=True, env=env)
    feat_env = {**env, "GIT_AUTHOR_DATE": "2026-05-03T12:00:00Z", "GIT_COMMITTER_DATE": "2026-05-03T12:00:00Z"}
    subprocess.run(["git", "commit", "-q", "-m", "add foo"], cwd=repo, check=True, env=feat_env)

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True, env=env)
    merge_env = {**env, "GIT_AUTHOR_DATE": "2026-05-03T12:00:00Z", "GIT_COMMITTER_DATE": "2026-05-08T18:00:00Z"}
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "-m", "merge feature", "feature"],
        cwd=repo,
        check=True,
        env=merge_env,
    )

    dt = _get_entry_added_datetime(repo / "libs" / "mngr" / "changelog" / "foo.md", repo)
    assert dt.strftime("%Y-%m-%d") == "2026-05-08"
    assert dt.strftime("%H") == "11"


def test_get_entry_added_datetime_raises_when_file_not_in_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(repo, [("placeholder.txt", "x\n", "2026-05-01T00:00:00Z")])
    untracked = repo / "libs" / "mngr" / "changelog" / "fresh.md"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("- new\n")
    with pytest.raises(RuntimeError, match="no commit found"):
        _get_entry_added_datetime(untracked, repo)


def test_group_entries_by_date_groups_by_committed_pt_date(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(
        repo,
        [
            ("libs/mngr/changelog/old.md", "- old entry\n", "2026-05-01T12:00:00Z"),
            ("libs/mngr/changelog/mid.md", "- mid entry\n", "2026-05-05T12:00:00Z"),
            ("libs/mngr/changelog/new.md", "- new entry\n", "2026-05-08T12:00:00Z"),
        ],
    )
    entries = _collect_project_entries(repo / "libs" / "mngr" / "changelog")
    by_date = _group_entries_by_date(entries, repo)
    assert sorted(by_date.keys()) == ["2026-05-01", "2026-05-05", "2026-05-08"]
    assert [p.name for p, _ in by_date["2026-05-08"]] == ["new.md"]


def test_group_entries_by_date_combines_same_day(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(
        repo,
        [
            ("libs/mngr/changelog/b.md", "- b entry\n", "2026-05-08T11:00:00Z"),
            ("libs/mngr/changelog/a.md", "- a entry\n", "2026-05-08T17:00:00Z"),
        ],
    )
    entries = _collect_project_entries(repo / "libs" / "mngr" / "changelog")
    by_date = _group_entries_by_date(entries, repo)
    assert list(by_date.keys()) == ["2026-05-08"]
    assert [p.name for p, _ in by_date["2026-05-08"]] == ["a.md", "b.md"]


def test_group_entries_by_date_uses_pacific_timezone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(
        repo,
        [
            ("libs/mngr/changelog/late.md", "- late\n", "2026-05-09T03:00:00Z"),
        ],
    )
    entries = _collect_project_entries(repo / "libs" / "mngr" / "changelog")
    by_date = _group_entries_by_date(entries, repo)
    assert list(by_date.keys()) == ["2026-05-08"]


def test_get_entry_added_datetime_raises_when_not_a_git_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    stray = not_a_repo / "libs" / "mngr" / "changelog" / "stray.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("- stray\n")
    with pytest.raises(RuntimeError, match="git log failed"):
        _get_entry_added_datetime(stray, not_a_repo)


def test_consolidate_project_errors_when_unabridged_missing(tmp_path: Path) -> None:
    """If a project has pending entries but its UNABRIDGED_CHANGELOG.md doesn't exist,
    refuse to consolidate rather than silently creating a new file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_libs_project(repo, "mngr")
    _init_git_repo_with_files(
        repo,
        [
            ("libs/mngr/changelog/foo.md", "- foo\n", "2026-05-08T12:00:00Z"),
        ],
    )
    with pytest.raises(FileNotFoundError, match="missing.*UNABRIDGED_CHANGELOG.md"):
        _consolidate_project("mngr", repo)


def test_consolidate_project_routes_to_project_unabridged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = _seed_libs_project(repo, "mngr")
    (project / "UNABRIDGED_CHANGELOG.md").write_text("# Unabridged Changelog - mngr\n\nIntro.\n")
    _init_git_repo_with_files(
        repo,
        [
            ("libs/mngr/changelog/foo.md", "- foo\n", "2026-05-08T12:00:00Z"),
        ],
    )

    dates_added, entry_names = _consolidate_project("mngr", repo)
    assert dates_added == ["2026-05-08"]
    assert entry_names == ["foo.md"]
    assert not (project / "changelog" / "foo.md").exists()
    unabridged = (project / "UNABRIDGED_CHANGELOG.md").read_text()
    assert "## 2026-05-08\n\n- foo\n" in unabridged
    assert unabridged.startswith("# Unabridged Changelog - mngr\n\nIntro.")
