from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.config.data_types import DEFAULT_CLOUDFLARE_FORWARDING_URL
from imbue.minds.config.data_types import DEFAULT_SUPERTOKENS_CONNECTION_URI
from imbue.minds.config.loader import CONFIG_FILENAME
from imbue.minds.config.loader import load_minds_config
from imbue.minds.errors import MindsConfigError


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no stray env-var overrides leak into loader tests."""
    monkeypatch.delenv("CLOUDFLARE_FORWARDING_URL", raising=False)
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    config = load_minds_config(tmp_path)
    assert config.cloudflare_forwarding_url == AnyUrl(DEFAULT_CLOUDFLARE_FORWARDING_URL)
    assert config.supertokens_connection_uri == AnyUrl(DEFAULT_SUPERTOKENS_CONNECTION_URI)


def test_file_overrides_default(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text('cloudflare_forwarding_url = "https://cf-from-file.example.com/"\n')
    config = load_minds_config(tmp_path)
    assert str(config.cloudflare_forwarding_url) == "https://cf-from-file.example.com/"
    # field not in file still uses default
    assert config.supertokens_connection_uri == AnyUrl(DEFAULT_SUPERTOKENS_CONNECTION_URI)


def test_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / CONFIG_FILENAME).write_text('cloudflare_forwarding_url = "https://cf-from-file.example.com/"\n')
    monkeypatch.setenv("CLOUDFLARE_FORWARDING_URL", "https://cf-from-env.example.com/")
    config = load_minds_config(tmp_path)
    assert str(config.cloudflare_forwarding_url) == "https://cf-from-env.example.com/"


def test_env_overrides_default_with_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st-from-env.example.com/")
    config = load_minds_config(tmp_path)
    assert str(config.supertokens_connection_uri) == "https://st-from-env.example.com/"
    # field not in env uses default
    assert config.cloudflare_forwarding_url == AnyUrl(DEFAULT_CLOUDFLARE_FORWARDING_URL)


def test_invalid_url_in_file_raises(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text('cloudflare_forwarding_url = "not-a-url"\n')
    with pytest.raises(MindsConfigError):
        load_minds_config(tmp_path)


def test_invalid_url_in_env_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_FORWARDING_URL", "not-a-url")
    with pytest.raises(MindsConfigError):
        load_minds_config(tmp_path)


def test_malformed_toml_raises(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text("this is = broken = toml\n")
    with pytest.raises(MindsConfigError):
        load_minds_config(tmp_path)


def test_extra_key_in_file_raises(tmp_path: Path) -> None:
    """MindsConfig has extra='forbid'; unknown keys fail validation."""
    (tmp_path / CONFIG_FILENAME).write_text('unknown_field = "x"\n')
    with pytest.raises(MindsConfigError):
        load_minds_config(tmp_path)
