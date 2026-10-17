from pathlib import Path

import tomlkit

from imbue.minds.mngr_settings.file_store import FileMindsSettingsStore


def test_read_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    store = FileMindsSettingsStore(settings_path=tmp_path / "settings.toml")
    assert store.read() == {}


def test_update_writes_only_when_mutator_reports_changes(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.toml"
    store = FileMindsSettingsStore(settings_path=settings_path)

    def leave_unchanged(doc: tomlkit.TOMLDocument) -> bool:
        return False

    assert store.update(leave_unchanged) is False
    assert not settings_path.exists()

    def add_value(doc: tomlkit.TOMLDocument) -> bool:
        doc["marker"] = "value-73194"
        return True

    assert store.update(add_value) is True
    assert store.read() == {"marker": "value-73194"}


def test_update_preserves_unrelated_content(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text('# user comment\nexisting = "kept-51826"\n')
    store = FileMindsSettingsStore(settings_path=settings_path)

    def add_value(doc: tomlkit.TOMLDocument) -> bool:
        doc["added"] = "new-90417"
        return True

    store.update(add_value)
    text = settings_path.read_text()
    assert "# user comment" in text
    assert store.read() == {"existing": "kept-51826", "added": "new-90417"}
