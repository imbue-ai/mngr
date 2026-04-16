from pathlib import Path

from imbue.minds.desktop_client.minds_config import MindsConfig


def _make_config(tmp_path: Path) -> MindsConfig:
    return MindsConfig(data_dir=tmp_path)


def test_default_values_when_no_file(tmp_path: Path) -> None:
    """Default values are returned when config.toml does not exist."""
    config = _make_config(tmp_path)
    assert config.get_default_account_id() is None
    assert config.get_auto_open_requests_panel() is True


def test_set_and_get_default_account_id(tmp_path: Path) -> None:
    """Setting and getting default_account_id works correctly."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-123")
    assert config.get_default_account_id() == "user-123"


def test_clear_default_account_id(tmp_path: Path) -> None:
    """Clearing the default account sets it to None."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-123")
    config.set_default_account_id(None)
    assert config.get_default_account_id() is None


def test_set_and_get_auto_open_requests_panel(tmp_path: Path) -> None:
    """Setting auto_open_requests_panel persists correctly."""
    config = _make_config(tmp_path)
    config.set_auto_open_requests_panel(False)
    assert config.get_auto_open_requests_panel() is False

    config.set_auto_open_requests_panel(True)
    assert config.get_auto_open_requests_panel() is True


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Config written by one instance is readable by a new instance."""
    config1 = _make_config(tmp_path)
    config1.set_default_account_id("user-abc")
    config1.set_auto_open_requests_panel(False)

    config2 = _make_config(tmp_path)
    assert config2.get_default_account_id() == "user-abc"
    assert config2.get_auto_open_requests_panel() is False


def test_corrupt_toml_returns_defaults(tmp_path: Path) -> None:
    """A corrupt config.toml is handled gracefully with defaults."""
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text("not valid toml {{{")
    assert config.get_default_account_id() is None
    assert config.get_auto_open_requests_panel() is True


def test_multiple_settings_coexist(tmp_path: Path) -> None:
    """Setting one value does not clobber other values."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-xyz")
    config.set_auto_open_requests_panel(False)

    assert config.get_default_account_id() == "user-xyz"
    assert config.get_auto_open_requests_panel() is False

    config.set_default_account_id("user-new")
    assert config.get_auto_open_requests_panel() is False
