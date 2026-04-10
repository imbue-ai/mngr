from pathlib import Path

import pytest

from scripts.consolidate_changelog import _build_new_section
from scripts.consolidate_changelog import _collect_entries
from scripts.consolidate_changelog import _insert_section_into_changelog


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
