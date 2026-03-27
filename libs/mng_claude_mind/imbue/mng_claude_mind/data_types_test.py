"""Unit tests for claude mind data types."""

import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mng_claude_mind.data_types import ClaudeMindSettings
from imbue.mng_claude_mind.data_types import VendorRepoConfig
from imbue.mng_mind.data_types import WatcherSettings


def test_claude_mind_settings_defaults() -> None:
    settings = ClaudeMindSettings()
    assert settings.agent_type is None
    assert settings.watchers == WatcherSettings()
    assert settings.vendor == ()


def test_claude_mind_settings_with_agent_type() -> None:
    settings = ClaudeMindSettings.model_validate({"agent_type": "claude-mind"})
    assert settings.agent_type == "claude-mind"


def test_claude_mind_settings_with_vendor_config() -> None:
    settings = ClaudeMindSettings.model_validate(
        {
            "vendor": [
                {"name": "mng", "url": "https://github.com/imbue-ai/mng.git"},
                {"name": "my-lib", "path": "/some/local/path"},
            ],
        }
    )
    assert len(settings.vendor) == 2
    assert settings.vendor[0].name == "mng"
    assert settings.vendor[0].url == "https://github.com/imbue-ai/mng.git"
    assert settings.vendor[0].is_local is False
    assert settings.vendor[1].name == "my-lib"
    assert settings.vendor[1].path == "/some/local/path"
    assert settings.vendor[1].is_local is True


def test_vendor_repo_config_requires_url_or_path() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        VendorRepoConfig(name=NonEmptyStr("bad"))


def test_vendor_repo_config_rejects_both_url_and_path() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        VendorRepoConfig(name=NonEmptyStr("bad"), url="https://example.com/repo.git", path="/local")


def test_vendor_repo_config_with_ref() -> None:
    config = VendorRepoConfig(name=NonEmptyStr("pinned"), url="https://example.com/repo.git", ref="abc123")
    assert config.ref == "abc123"


def test_vendor_repo_config_ref_defaults_to_none() -> None:
    config = VendorRepoConfig(name=NonEmptyStr("latest"), url="https://example.com/repo.git")
    assert config.ref is None
