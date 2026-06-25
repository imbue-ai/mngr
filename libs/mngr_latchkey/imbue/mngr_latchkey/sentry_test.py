import pytest

from imbue.imbue_common.sentry.data_types import SentryDeployEnvironment
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR
from imbue.mngr_latchkey.sentry import resolve_forward_sentry_config


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, "staging")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR, "1.2.3")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR, "abc123")


def test_resolve_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR, raising=False)
    assert resolve_forward_sentry_config() is None


def test_resolve_returns_config_when_fully_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR, "1")
    config = resolve_forward_sentry_config()
    assert config is not None
    assert config.environment is SentryDeployEnvironment.STAGING
    assert config.release_id == "1.2.3"
    assert config.git_commit_sha == "abc123"
    assert config.is_s3_upload_enabled is True


def test_resolve_defaults_s3_upload_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv(MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR, raising=False)
    config = resolve_forward_sentry_config()
    assert config is not None
    assert config.is_s3_upload_enabled is False


@pytest.mark.parametrize(
    "missing_env_var_name",
    [
        MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR,
        MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR,
        MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR,
    ],
)
def test_resolve_returns_none_when_required_env_var_missing(
    monkeypatch: pytest.MonkeyPatch, missing_env_var_name: str
) -> None:
    # Enabled but missing a required input -> disabled (no placeholder fallback).
    _set_required_env(monkeypatch)
    monkeypatch.delenv(missing_env_var_name, raising=False)
    assert resolve_forward_sentry_config() is None


def test_resolve_returns_none_when_environment_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, "not-a-real-env")
    assert resolve_forward_sentry_config() is None
