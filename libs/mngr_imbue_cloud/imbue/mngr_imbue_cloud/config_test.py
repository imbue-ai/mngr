from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.mngr_imbue_cloud.config import ACCOUNTS_URL_ENV_VAR
from imbue.mngr_imbue_cloud.config import CONNECTOR_URL_ENV_VAR
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import MissingConnectorUrlError
from imbue.mngr_imbue_cloud.config import get_active_profile_dir
from imbue.mngr_imbue_cloud.config import get_provider_data_dir
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount


def test_provider_data_dir_uses_standard_layout() -> None:
    data_dir = get_provider_data_dir(Path("/some/profile_dir"), "imbue_cloud_alice")
    assert data_dir == Path("/some/profile_dir/providers/imbue_cloud/imbue_cloud_alice")


def test_sessions_dir_is_one_level_up_from_instance() -> None:
    sessions = get_sessions_dir(Path("/some/profile_dir"))
    assert sessions == Path("/some/profile_dir/providers/imbue_cloud/sessions")
    # Multiple instances share this dir; the path is independent of instance name.


def test_get_active_profile_dir_resolves_profile(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('profile = "profile0"\n')
    assert get_active_profile_dir(tmp_path) == tmp_path / "profiles" / "profile0"


def test_get_active_profile_dir_wraps_malformed_config(tmp_path: Path) -> None:
    """A torn / hand-edited config.toml surfaces as ImbueCloudError, not a raw TOMLDecodeError.

    Callers (e.g. the minds desktop client's render-path identity cache) only
    handle ImbueCloudError, so a decode failure must not leak through.
    """
    (tmp_path / "config.toml").write_text('profile = "unterminated')
    with pytest.raises(ImbueCloudError):
        get_active_profile_dir(tmp_path)


def test_get_active_profile_dir_wraps_non_utf8_config(tmp_path: Path) -> None:
    """A corrupt (non-UTF-8) config.toml surfaces as ImbueCloudError, not a raw UnicodeDecodeError."""
    (tmp_path / "config.toml").write_bytes(b"\xff\xfeprofile")
    with pytest.raises(ImbueCloudError):
        get_active_profile_dir(tmp_path)


def test_get_active_profile_dir_rejects_non_string_profile(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("profile = 123\n")
    with pytest.raises(ImbueCloudError):
        get_active_profile_dir(tmp_path)


@pytest.mark.parametrize("env_var", [CONNECTOR_URL_ENV_VAR, ACCOUNTS_URL_ENV_VAR])
def test_provider_env_override_vars_map_to_declared_config_fields(env_var: str) -> None:
    """mngr parses every MNGR__PROVIDERS__IMBUE_CLOUD__<FIELD> env var as a
    providers.imbue_cloud.<field> config override and rejects unknown fields in
    strict mode (the default), so each env var this plugin documents must map to
    a declared field -- otherwise exporting it breaks every full mngr command
    with a ConfigParseError."""
    field_name = env_var.rsplit("__", 1)[-1].lower()
    assert field_name in ImbueCloudProviderConfig.model_fields


def test_get_connector_url_uses_explicit_field_when_set() -> None:
    config = ImbueCloudProviderConfig(
        account=ImbueCloudAccount("a@b.com"),
        connector_url=AnyUrl("https://override.example.com/"),
    )
    assert config.get_connector_url() == "https://override.example.com"


def test_get_connector_url_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONNECTOR_URL_ENV_VAR, "https://env.example.com/")
    config = ImbueCloudProviderConfig(account=ImbueCloudAccount("a@b.com"))
    assert config.get_connector_url() == "https://env.example.com"


def test_get_connector_url_raises_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no field and no env var, the resolver raises -- there is no baked default."""
    monkeypatch.delenv(CONNECTOR_URL_ENV_VAR, raising=False)
    config = ImbueCloudProviderConfig(account=ImbueCloudAccount("a@b.com"))
    with pytest.raises(MissingConnectorUrlError):
        config.get_connector_url()
