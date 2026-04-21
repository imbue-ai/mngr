from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.desktop_client.minds_config import DEFAULT_CLOUDFLARE_FORWARDING_URL
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.errors import MindsConfigError


@pytest.fixture(autouse=True)
def _clear_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no stray env-var overrides leak into url-resolution tests."""
    monkeypatch.delenv("CLOUDFLARE_FORWARDING_URL", raising=False)


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


# -- URL settings (env > file > default) --


def test_cloudflare_forwarding_url_defaults_when_unset(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.cloudflare_forwarding_url == AnyUrl(DEFAULT_CLOUDFLARE_FORWARDING_URL)


def test_cloudflare_forwarding_url_file_overrides_default(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('cloudflare_forwarding_url = "https://cf-from-file.example.com/"\n')
    config = _make_config(tmp_path)
    assert str(config.cloudflare_forwarding_url) == "https://cf-from-file.example.com/"


def test_cloudflare_forwarding_url_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.toml").write_text('cloudflare_forwarding_url = "https://cf-from-file.example.com/"\n')
    monkeypatch.setenv("CLOUDFLARE_FORWARDING_URL", "https://cf-from-env.example.com/")
    config = _make_config(tmp_path)
    assert str(config.cloudflare_forwarding_url) == "https://cf-from-env.example.com/"


def test_invalid_url_in_file_raises(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('cloudflare_forwarding_url = "not-a-url"\n')
    config = _make_config(tmp_path)
    with pytest.raises(MindsConfigError):
        _ = config.cloudflare_forwarding_url


def test_invalid_url_in_env_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_FORWARDING_URL", "not-a-url")
    config = _make_config(tmp_path)
    with pytest.raises(MindsConfigError):
        _ = config.cloudflare_forwarding_url


def test_url_settings_coexist_with_user_preferences(tmp_path: Path) -> None:
    """Setting a user preference does not clobber URL config, and vice versa."""
    (tmp_path / "config.toml").write_text(
        'cloudflare_forwarding_url = "https://cf.example.com/"\ndefault_account_id = "user-1"\n'
    )
    config = _make_config(tmp_path)
    assert str(config.cloudflare_forwarding_url) == "https://cf.example.com/"
    assert config.get_default_account_id() == "user-1"
    # Writing a user preference preserves the URL config on re-read.
    config.set_auto_open_requests_panel(False)
    assert str(config.cloudflare_forwarding_url) == "https://cf.example.com/"
