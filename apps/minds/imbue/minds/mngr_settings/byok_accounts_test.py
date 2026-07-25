import pytest

from imbue.minds.mngr_settings.byok_accounts import is_bring_your_own_cloud_enabled


def test_is_bring_your_own_cloud_enabled_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off by default (env absent), on only for the exact "1" value -- the feature
    # ships dark behind FEATURE_FLAG_BRING_YOUR_OWN_CLOUDS.
    monkeypatch.delenv("FEATURE_FLAG_BRING_YOUR_OWN_CLOUDS", raising=False)
    assert is_bring_your_own_cloud_enabled() is False
    monkeypatch.setenv("FEATURE_FLAG_BRING_YOUR_OWN_CLOUDS", "1")
    assert is_bring_your_own_cloud_enabled() is True
    for off_value in ("0", "", "true", "yes"):
        monkeypatch.setenv("FEATURE_FLAG_BRING_YOUR_OWN_CLOUDS", off_value)
        assert is_bring_your_own_cloud_enabled() is False
