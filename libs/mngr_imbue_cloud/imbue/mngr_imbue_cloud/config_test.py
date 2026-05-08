from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.mngr_imbue_cloud.config import CONNECTOR_URL_ENV_VAR
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_provider_data_dir
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount


def test_provider_data_dir_uses_standard_layout() -> None:
    data_dir = get_provider_data_dir(Path("/some/profile_dir"), "imbue_cloud_alice")
    assert data_dir == Path("/some/profile_dir/providers/imbue_cloud/imbue_cloud_alice")


def test_sessions_dir_is_one_level_up_from_instance() -> None:
    sessions = get_sessions_dir(Path("/some/profile_dir"))
    assert sessions == Path("/some/profile_dir/providers/imbue_cloud/sessions")
    # Multiple instances share this dir; the path is independent of instance name.


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


def test_get_connector_url_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONNECTOR_URL_ENV_VAR, raising=False)
    config = ImbueCloudProviderConfig(account=ImbueCloudAccount("a@b.com"))
    url = config.get_connector_url()
    # Sanity-check shape; the exact value is the baked-in production URL.
    assert url.startswith("http")
    assert IMBUE_CLOUD_BACKEND_NAME != ""  # tautology, just to anchor the import
