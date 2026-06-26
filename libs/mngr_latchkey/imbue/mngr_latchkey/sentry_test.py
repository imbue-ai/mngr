import pytest

from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR
from imbue.mngr_latchkey.sentry import resolve_forward_sentry_config

_FAKE_DSN = "https://public@example.com/1"


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR, _FAKE_DSN)
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, "staging")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR, "1.2.3")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR, "abc123")


def test_resolve_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR, raising=False)
    assert resolve_forward_sentry_config() is None


def test_resolve_returns_config_when_fully_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR, "traceback-uploads-staging")
    config = resolve_forward_sentry_config()
    assert config is not None
    assert config.dsn == _FAKE_DSN
    assert config.environment_name == "staging"
    assert config.release_id == "1.2.3"
    assert config.git_commit_sha == "abc123"
    assert config.s3_attachment_bucket == "traceback-uploads-staging"


def test_resolve_defaults_s3_bucket_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv(MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR, raising=False)
    config = resolve_forward_sentry_config()
    assert config is not None
    assert config.s3_attachment_bucket is None


def test_resolve_treats_empty_s3_bucket_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR, "")
    config = resolve_forward_sentry_config()
    assert config is not None
    assert config.s3_attachment_bucket is None


@pytest.mark.parametrize(
    "missing_env_var_name",
    [
        MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR,
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
