from pathlib import Path

import pytest

from scripts.changelog_release_utils import finalize_changelog_unreleased


def test_finalize_renames_unreleased_and_inserts_fresh_one(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\nIntro text.\n\n## [Unreleased]\n\n### Added\n- New feature\n\n### Fixed\n- A bug\n"
    )
    had_content = finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")
    assert had_content is True
    result = changelog.read_text()
    # New [Unreleased] sits above the new versioned heading
    assert "## [Unreleased]\n\n## [v1.2.3] - 2026-05-11" in result
    # Existing bullets stay attached to the versioned heading
    assert "## [v1.2.3] - 2026-05-11\n\n### Added\n- New feature" in result
    # Intro text preserved
    assert result.startswith("# Changelog\n\nIntro text.\n\n## [Unreleased]")


def test_finalize_with_prior_versioned_section_keeps_order(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "### Changed\n"
        "- Refactor\n"
        "\n"
        "## [v1.2.2] - 2026-05-01\n"
        "\n"
        "### Added\n"
        "- Old feature\n"
    )
    finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")
    result = changelog.read_text()
    idx_unreleased = result.index("## [Unreleased]")
    idx_new = result.index("## [v1.2.3] - 2026-05-11")
    idx_old = result.index("## [v1.2.2] - 2026-05-01")
    assert idx_unreleased < idx_new < idx_old


def test_finalize_returns_false_when_unreleased_is_empty(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n## [v1.2.2] - 2026-05-01\n\n- old\n")
    had_content = finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")
    assert had_content is False
    result = changelog.read_text()
    # Versioned section is still emitted (the invariant: every release has a heading)
    assert "## [v1.2.3] - 2026-05-11" in result
    # New [Unreleased] is still present
    assert "## [Unreleased]" in result


def test_finalize_returns_false_when_unreleased_only_has_blank_lines(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n\n   \n\n## [v1.0.0] - 2026-01-01\n")
    had_content = finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")
    assert had_content is False


def test_finalize_returns_true_when_unreleased_extends_to_eof(tmp_path: Path) -> None:
    """If [Unreleased] is the only section in the file, finalize still works
    and treats trailing bullets as content."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n### Added\n- new\n")
    had_content = finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")
    assert had_content is True
    result = changelog.read_text()
    assert "## [Unreleased]\n\n## [v1.2.3] - 2026-05-11" in result


def test_finalize_errors_when_unreleased_missing(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [v1.0.0] - 2026-01-01\n\n- old\n")
    with pytest.raises(RuntimeError, match=r"\[Unreleased\] heading not found"):
        finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")


def test_finalize_errors_when_multiple_unreleased(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n- a\n\n## [Unreleased]\n\n- b\n")
    with pytest.raises(RuntimeError, match=r"Multiple .* headings"):
        finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")


def test_finalize_errors_when_file_missing(tmp_path: Path) -> None:
    changelog = tmp_path / "missing.md"
    with pytest.raises(FileNotFoundError, match="Changelog file not found"):
        finalize_changelog_unreleased(changelog, "1.2.3", "2026-05-11")
