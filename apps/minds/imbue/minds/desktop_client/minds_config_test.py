from pathlib import Path

import pytest

from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.errors import MindsConfigError


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


def test_default_region_is_none(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.get_region("imbue_cloud") is None


def test_set_and_get_region_per_provider(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_region("imbue_cloud", "US-WEST-OR")
    config.set_region("vultr", "lhr")
    assert config.get_region("imbue_cloud") == "US-WEST-OR"
    assert config.get_region("vultr") == "lhr"
    # A provider with no stored region still reads as None.
    assert config.get_region("docker") is None


def test_set_region_overwrites_previous_value(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_region("imbue_cloud", "US-EAST-VA")
    config.set_region("imbue_cloud", "US-WEST-OR")
    assert config.get_region("imbue_cloud") == "US-WEST-OR"


def test_set_and_get_auto_open_requests_panel(tmp_path: Path) -> None:
    """Setting auto_open_requests_panel persists correctly."""
    config = _make_config(tmp_path)
    config.set_auto_open_requests_panel(False)
    assert config.get_auto_open_requests_panel() is False

    config.set_auto_open_requests_panel(True)
    assert config.get_auto_open_requests_panel() is True


def test_error_reporting_settings_default_off(tmp_path: Path) -> None:
    """Consent and both error-reporting toggles default to off on a fresh install."""
    config = _make_config(tmp_path)
    assert config.get_error_reporting_consent_given() is False
    assert config.get_report_unexpected_errors() is False
    assert config.get_include_error_logs() is False


def test_error_reporting_settings_round_trip(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_error_reporting_consent_given(True)
    config.set_report_unexpected_errors(True)
    config.set_include_error_logs(True)
    assert config.get_error_reporting_consent_given() is True
    assert config.get_report_unexpected_errors() is True
    assert config.get_include_error_logs() is True
    # A new instance reads the same persisted values.
    reloaded = _make_config(tmp_path)
    assert reloaded.get_error_reporting_consent_given() is True
    assert reloaded.get_report_unexpected_errors() is True
    assert reloaded.get_include_error_logs() is True


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Config written by one instance is readable by a new instance."""
    config1 = _make_config(tmp_path)
    config1.set_default_account_id("user-abc")
    config1.set_auto_open_requests_panel(False)

    config2 = _make_config(tmp_path)
    assert config2.get_default_account_id() == "user-abc"
    assert config2.get_auto_open_requests_panel() is False


def test_corrupt_toml_raises(tmp_path: Path) -> None:
    """A corrupt config.toml raises MindsConfigError rather than silently
    returning defaults. Silent fallback would hide data corruption -- e.g.
    the next ``set_*`` call would overwrite the unparseable file with a
    fresh one derived from an empty dict, losing whatever the user had
    intended to be stored.
    """
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text("not valid toml {{{")
    with pytest.raises(MindsConfigError):
        config.get_default_account_id()
    with pytest.raises(MindsConfigError):
        config.get_auto_open_requests_panel()


def test_multiple_settings_coexist(tmp_path: Path) -> None:
    """Setting one value does not clobber other values."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-xyz")
    config.set_auto_open_requests_panel(False)

    assert config.get_default_account_id() == "user-xyz"
    assert config.get_auto_open_requests_panel() is False

    config.set_default_account_id("user-new")
    assert config.get_auto_open_requests_panel() is False
