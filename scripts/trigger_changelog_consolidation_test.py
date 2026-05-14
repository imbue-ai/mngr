from pathlib import Path

from scripts.trigger_changelog_consolidation import _ENABLED_PLUGINS
from scripts.trigger_changelog_consolidation import disable_plugin_args
from scripts.trigger_changelog_consolidation import gate_release_on_pending_entries
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


def test_gate_passes_when_no_pending_entries(tmp_path: Path) -> None:
    def fail_input(_: str) -> str:
        raise AssertionError("input should not be called when no entries are pending")

    def fail_trigger(_: str) -> int:
        raise AssertionError("trigger should not be called when no entries are pending")

    assert gate_release_on_pending_entries(tmp_path, input_fn=fail_input, run_trigger_fn=fail_trigger) is True


def test_gate_blocks_and_skips_trigger_when_user_declines(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "foo.md").write_text("entry")

    trigger_calls: list[str] = []

    def trigger_recorder(provider: str) -> int:
        trigger_calls.append(provider)
        return 0

    assert (
        gate_release_on_pending_entries(
            tmp_path,
            input_fn=lambda _: "n",
            run_trigger_fn=trigger_recorder,
        )
        is False
    )
    assert trigger_calls == []


def test_gate_blocks_and_invokes_trigger_when_user_accepts(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "foo.md").write_text("entry")
    (changelog_dir / "bar.md").write_text("entry")

    trigger_calls: list[str] = []

    def trigger_recorder(provider: str) -> int:
        trigger_calls.append(provider)
        return 0

    assert (
        gate_release_on_pending_entries(
            tmp_path,
            provider="local",
            input_fn=lambda _: "y",
            run_trigger_fn=trigger_recorder,
        )
        is False
    )
    # The gate always returns False when entries are pending so the caller
    # re-runs after the consolidation PR lands.
    assert trigger_calls == ["local"]


def test_gate_blocks_when_trigger_fails(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "foo.md").write_text("entry")

    def failing_trigger(_: str) -> int:
        return 2

    assert (
        gate_release_on_pending_entries(
            tmp_path,
            input_fn=lambda _: "y",
            run_trigger_fn=failing_trigger,
        )
        is False
    )


def test_gate_warns_but_passes_in_dry_run(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "foo.md").write_text("entry")

    def fail_input(_: str) -> str:
        raise AssertionError("input should not be called in dry_run mode")

    def fail_trigger(_: str) -> int:
        raise AssertionError("trigger should not be called in dry_run mode")

    assert (
        gate_release_on_pending_entries(
            tmp_path,
            dry_run=True,
            input_fn=fail_input,
            run_trigger_fn=fail_trigger,
        )
        is True
    )


def test_gate_treats_uppercase_y_as_accept(tmp_path: Path) -> None:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    (changelog_dir / "foo.md").write_text("entry")

    trigger_calls: list[str] = []

    def trigger_recorder(provider: str) -> int:
        trigger_calls.append(provider)
        return 0

    gate_release_on_pending_entries(tmp_path, input_fn=lambda _: "  Y\n", run_trigger_fn=trigger_recorder)
    assert trigger_calls == ["modal"]
