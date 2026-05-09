import os
import subprocess
from pathlib import Path

import pytest

from scripts.consolidate_changelog import _build_new_section
from scripts.consolidate_changelog import _collect_entries
from scripts.consolidate_changelog import _get_entry_added_datetime
from scripts.consolidate_changelog import _insert_section_into_changelog
from scripts.consolidate_changelog import _latest_entry_date_str


def test_collect_entries_empty_dir(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / ".gitkeep").touch()
    assert _collect_entries(changelog_dir) == []


def test_collect_entries_skips_non_md_files(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "notes.txt").write_text("not a changelog entry")
    assert _collect_entries(changelog_dir) == []


def test_collect_entries_skips_empty_content(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "empty.md").write_text("   \n\n  ")
    assert _collect_entries(changelog_dir) == []


def test_collect_entries_returns_sorted_entries(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "b-feature.md").write_text("- Feature B")
    (changelog_dir / "a-bugfix.md").write_text("- Bugfix A")
    entries = _collect_entries(changelog_dir)
    assert len(entries) == 2
    assert entries[0][0].name == "a-bugfix.md"
    assert entries[0][1] == "- Bugfix A"
    assert entries[1][0].name == "b-feature.md"
    assert entries[1][1] == "- Feature B"


def test_build_new_section_single_entry() -> None:
    entries = [(Path("a.md"), "- Added feature X")]
    result = _build_new_section("2026-04-02", entries)
    assert result == "## 2026-04-02\n\n- Added feature X\n"


def test_build_new_section_multiple_entries() -> None:
    entries = [
        (Path("a.md"), "- Feature A"),
        (Path("b.md"), "- Feature B"),
    ]
    result = _build_new_section("2026-04-02", entries)
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


def _init_git_repo_with_files(repo: Path, files_with_dates: list[tuple[str, str, str]]) -> None:
    """Init a temp git repo and commit each file at its given author date.

    files_with_dates: list of (rel_path, content, iso_date) tuples. Each entry
    is added in its own commit so git log --diff-filter=A --format=%aI returns
    the per-file date.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
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


def test_get_entry_added_datetime_uses_git_author_date(tmp_path: Path) -> None:
    """The helper returns the author date of the commit that added the file, in PT."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(
        repo,
        [
            # 2026-05-08T18:00:00Z = 2026-05-08T11:00:00 PT (PDT, UTC-7)
            ("changelog/foo.md", "- entry foo\n", "2026-05-08T18:00:00Z"),
        ],
    )
    dt = _get_entry_added_datetime(repo / "changelog" / "foo.md", repo)
    assert dt.strftime("%Y-%m-%d") == "2026-05-08"
    assert dt.strftime("%H") == "11"


def test_get_entry_added_datetime_falls_back_to_mtime_when_uncommitted(tmp_path: Path) -> None:
    """If git has no record of the file, fall back to its mtime."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(repo, [("placeholder.txt", "x\n", "2026-05-01T00:00:00Z")])
    untracked = repo / "changelog" / "fresh.md"
    untracked.parent.mkdir()
    untracked.write_text("- new\n")
    dt = _get_entry_added_datetime(untracked, repo)
    # Mtime fallback: just check we got an aware datetime in PT
    assert dt.tzinfo is not None
    assert dt.tzinfo.key == "America/Los_Angeles"


def test_latest_entry_date_str_picks_most_recent(tmp_path: Path) -> None:
    """When entries have different add dates, pick the most recent (in PT)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(
        repo,
        [
            ("changelog/old.md", "- old entry\n", "2026-05-01T12:00:00Z"),
            ("changelog/mid.md", "- mid entry\n", "2026-05-05T12:00:00Z"),
            ("changelog/new.md", "- new entry\n", "2026-05-08T12:00:00Z"),
        ],
    )
    entries = _collect_entries(repo / "changelog")
    assert _latest_entry_date_str(entries, repo) == "2026-05-08"


def test_get_entry_added_datetime_raises_when_not_a_git_repo(tmp_path: Path) -> None:
    """If the directory isn't a git repo at all, raise rather than silently using mtime."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    stray = not_a_repo / "changelog" / "stray.md"
    stray.parent.mkdir()
    stray.write_text("- stray\n")
    with pytest.raises(RuntimeError, match="git log failed"):
        _get_entry_added_datetime(stray, not_a_repo)


def test_latest_entry_date_str_uses_pacific_timezone(tmp_path: Path) -> None:
    """A UTC midnight entry that's still 'yesterday' in PT keeps the PT date."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_files(
        repo,
        [
            # 2026-05-09T03:00:00Z = 2026-05-08T20:00:00 PT (PDT)
            ("changelog/late.md", "- late\n", "2026-05-09T03:00:00Z"),
        ],
    )
    entries = _collect_entries(repo / "changelog")
    assert _latest_entry_date_str(entries, repo) == "2026-05-08"
