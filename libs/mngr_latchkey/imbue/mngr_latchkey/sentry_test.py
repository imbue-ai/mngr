from pathlib import Path

import pytest

from imbue.mngr_latchkey.sentry import ForwardSentryConsent
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR
from imbue.mngr_latchkey.sentry import read_forward_sentry_consent
from imbue.mngr_latchkey.sentry import resolve_forward_sentry_config

_FAKE_DSN = "https://public@example.com/1"


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR, _FAKE_DSN)
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, "staging")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR, "1.2.3")
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR, "abc123")


def test_resolve_returns_none_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR,
        MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR,
        MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR,
        MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    assert resolve_forward_sentry_config() is None


def test_resolve_returns_config_when_fully_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR, "traceback-uploads-staging")
    consent_path = tmp_path / "consent.json"
    monkeypatch.setenv(MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR, str(consent_path))
    config = resolve_forward_sentry_config()
    assert config is not None
    assert config.dsn == _FAKE_DSN
    assert config.environment_name == "staging"
    assert config.release_id == "1.2.3"
    assert config.git_commit_sha == "abc123"
    assert config.s3_attachment_bucket == "traceback-uploads-staging"
    assert config.consent_file_path == consent_path


def test_resolve_defaults_bucket_and_consent_file_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv(MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR, raising=False)
    monkeypatch.delenv(MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR, raising=False)
    config = resolve_forward_sentry_config()
    assert config is not None
    assert config.s3_attachment_bucket is None
    assert config.consent_file_path is None


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
    # Missing a required input -> not configured for Sentry (no placeholder fallback).
    _set_required_env(monkeypatch)
    monkeypatch.delenv(missing_env_var_name, raising=False)
    assert resolve_forward_sentry_config() is None


def test_read_consent_none_path_is_all_off() -> None:
    consent = read_forward_sentry_consent(None)
    assert consent.report_unexpected_errors is False
    assert consent.include_error_logs is False


def test_read_consent_missing_file_is_all_off(tmp_path: Path) -> None:
    consent = read_forward_sentry_consent(tmp_path / "does-not-exist.json")
    assert consent.report_unexpected_errors is False
    assert consent.include_error_logs is False


def test_read_consent_malformed_file_is_all_off(tmp_path: Path) -> None:
    path = tmp_path / "consent.json"
    path.write_text("not json")
    consent = read_forward_sentry_consent(path)
    assert consent.report_unexpected_errors is False
    assert consent.include_error_logs is False


def test_read_consent_reflects_file_contents_live(tmp_path: Path) -> None:
    # The gate reads the file every call, so rewriting it changes what the next read returns -- this is
    # what lets a consent toggle reach the running daemon without a respawn.
    path = tmp_path / "consent.json"
    path.write_text(ForwardSentryConsent(report_unexpected_errors=True, include_error_logs=True).model_dump_json())
    enabled = read_forward_sentry_consent(path)
    assert enabled.report_unexpected_errors is True
    assert enabled.include_error_logs is True

    path.write_text(ForwardSentryConsent(report_unexpected_errors=False, include_error_logs=False).model_dump_json())
    revoked = read_forward_sentry_consent(path)
    assert revoked.report_unexpected_errors is False
    assert revoked.include_error_logs is False
