from pathlib import Path

from scripts.trigger_changelog_consolidation import _ENABLED_PLUGINS
from scripts.trigger_changelog_consolidation import disable_plugin_args
from scripts.trigger_changelog_consolidation import pending_changelog_entries


def test_pending_returns_empty_when_no_changelog_dir(tmp_path: Path) -> None:
    assert pending_changelog_entries(tmp_path) == []


def test_pending_ignores_gitkeep(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / ".gitkeep").write_text("")
    assert pending_changelog_entries(tmp_path) == []


def test_pending_ignores_non_md_files(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "notes.txt").write_text("not a changelog entry")
    (changelog_dir / "real.md").write_text("real entry")
    assert pending_changelog_entries(tmp_path) == [changelog_dir / "real.md"]


def test_pending_returns_md_files_sorted(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "zebra.md").write_text("z")
    (changelog_dir / "apple.md").write_text("a")
    (changelog_dir / "middle.md").write_text("m")
    result = pending_changelog_entries(tmp_path)
    assert [p.name for p in result] == ["apple.md", "middle.md", "zebra.md"]


def test_disable_plugin_args_returns_paired_flags() -> None:
    args = disable_plugin_args()
    # args should be a list of (--disable-plugin, NAME) pairs.
    assert len(args) % 2 == 0
    for i in range(0, len(args), 2):
        assert args[i] == "--disable-plugin"
        assert args[i + 1] != ""
    names = args[1::2]
    # The minimum-required plugins must never be disabled.
    assert _ENABLED_PLUGINS.isdisjoint(names)
    # Names should be unique (no double-disables).
    assert len(names) == len(set(names))
