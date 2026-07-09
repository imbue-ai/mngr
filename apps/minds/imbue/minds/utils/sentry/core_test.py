from pathlib import Path

import pytest

from imbue.imbue_common.sentry.core import ErrorAttachmentsS3Uploader
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.utils.sentry.core import PRODUCTION_UPLOADS_BUCKET
from imbue.minds.utils.sentry.core import SENTRY_DSN_DEV
from imbue.minds.utils.sentry.core import SENTRY_DSN_PRODUCTION
from imbue.minds.utils.sentry.core import SENTRY_DSN_STAGING
from imbue.minds.utils.sentry.core import STAGING_UPLOADS_BUCKET
from imbue.minds.utils.sentry.core import SentryDeployEnvironment
from imbue.minds.utils.sentry.core import _MINDS_LOG_ATTACHMENT_GROUPS
from imbue.minds.utils.sentry.core import _S3_ATTACHMENT_BUCKET_BY_ENVIRONMENT
from imbue.minds.utils.sentry.core import _SENTRY_DSN_BY_ENVIRONMENT
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.sentry.core import resolve_latchkey_forward_sentry_env
from imbue.minds.utils.sentry.core import resolve_sentry_environment
from imbue.minds.utils.sentry.core import sentry_deploy_environment_from_minds_env_name
from imbue.minds.utils.sentry.core import write_latchkey_forward_sentry_consent
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR
from imbue.mngr_latchkey.sentry import read_forward_sentry_consent
from imbue.mngr_latchkey.sentry import resolve_forward_sentry_config


def test_sentry_environment_from_minds_env_name_maps_production_and_staging() -> None:
    assert sentry_deploy_environment_from_minds_env_name("production") is SentryDeployEnvironment.PRODUCTION
    assert sentry_deploy_environment_from_minds_env_name("staging") is SentryDeployEnvironment.STAGING


@pytest.mark.parametrize("env_name", ["dev-josh-1", "ci-ephemeral", "", "Production", "STAGING", None])
def test_sentry_environment_from_minds_env_name_defaults_to_development(env_name: str | None) -> None:
    assert sentry_deploy_environment_from_minds_env_name(env_name) is SentryDeployEnvironment.DEVELOPMENT


def test_dsn_map_pairs_each_environment_with_a_distinct_dsn() -> None:
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.PRODUCTION] == SENTRY_DSN_PRODUCTION
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.STAGING] == SENTRY_DSN_STAGING
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.DEVELOPMENT] == SENTRY_DSN_DEV
    assert len({SENTRY_DSN_PRODUCTION, SENTRY_DSN_STAGING, SENTRY_DSN_DEV}) == 3


def test_s3_bucket_map_only_production_and_staging_have_buckets() -> None:
    assert _S3_ATTACHMENT_BUCKET_BY_ENVIRONMENT[SentryDeployEnvironment.PRODUCTION] == PRODUCTION_UPLOADS_BUCKET
    assert _S3_ATTACHMENT_BUCKET_BY_ENVIRONMENT[SentryDeployEnvironment.STAGING] == STAGING_UPLOADS_BUCKET
    assert _S3_ATTACHMENT_BUCKET_BY_ENVIRONMENT[SentryDeployEnvironment.DEVELOPMENT] is None


@pytest.mark.parametrize(
    ("root_name", "expected"),
    [
        ("minds", SentryDeployEnvironment.PRODUCTION),
        ("minds-staging", SentryDeployEnvironment.STAGING),
        ("minds-dev-someone", SentryDeployEnvironment.DEVELOPMENT),
    ],
)
def test_resolve_sentry_environment_follows_root_name(
    monkeypatch: pytest.MonkeyPatch, root_name: str, expected: SentryDeployEnvironment
) -> None:
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, root_name)
    assert resolve_sentry_environment() is expected


def test_resolve_sentry_environment_defaults_to_development_when_unactivated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR, raising=False)
    assert resolve_sentry_environment() is SentryDeployEnvironment.DEVELOPMENT


def test_resolve_latchkey_forward_sentry_env_round_trips_into_forward_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The env vars minds publishes for the daemon must be consumable by the daemon's own resolver,
    # yielding minds' resolved DSN + environment + bucket + consent-file path. Verified end-to-end here
    # so the two sides (publisher and consumer) cannot drift apart. The bucket is always published (it
    # is infrastructure, decoupled from consent); consent lives in the file.
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    consent_path = latchkey_forward_sentry_consent_path(tmp_path)
    published_env = resolve_latchkey_forward_sentry_env(consent_file_path=consent_path)
    assert published_env[MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR] == SENTRY_DSN_STAGING
    assert published_env[MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR] == SentryDeployEnvironment.STAGING.value
    assert published_env[MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR] == STAGING_UPLOADS_BUCKET
    assert published_env[MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR] == str(consent_path)

    for env_var_name, value in published_env.items():
        monkeypatch.setenv(env_var_name, value)
    forward_config = resolve_forward_sentry_config()
    assert forward_config is not None
    assert forward_config.dsn == SENTRY_DSN_STAGING
    assert forward_config.environment_name == SentryDeployEnvironment.STAGING.value
    assert forward_config.s3_attachment_bucket == STAGING_UPLOADS_BUCKET
    assert forward_config.consent_file_path == consent_path


def test_consent_file_round_trips_minds_settings_to_the_daemon_reader(tmp_path: Path) -> None:
    # What minds writes (from its consent settings) must be exactly what the daemon's live reader sees,
    # and rewriting it must change the next read -- this is the mechanism that propagates a grant/revoke
    # to the running daemon without respawning it.
    consent_path = latchkey_forward_sentry_consent_path(tmp_path)
    write_latchkey_forward_sentry_consent(consent_path, is_error_reporting_enabled=True, is_log_inclusion_enabled=True)
    granted = read_forward_sentry_consent(consent_path)
    assert granted.report_unexpected_errors is True
    assert granted.include_error_logs is True

    write_latchkey_forward_sentry_consent(
        consent_path, is_error_reporting_enabled=False, is_log_inclusion_enabled=False
    )
    revoked = read_forward_sentry_consent(consent_path)
    assert revoked.report_unexpected_errors is False
    assert revoked.include_error_logs is False


def test_collect_external_attachments_classifies_flat_minds_log_layout(tmp_path: Path) -> None:
    # The minds logs dir is flat: a live `*.jsonl`, timestamp-suffixed rotated
    # `*.jsonl.<ts>` logs, and the Electron `*.log`. Each must land in its own
    # group, and the globs must not cross-match (e.g. `*.jsonl` must not pick up
    # the rotated files).
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    (logs_folder / "minds-events.jsonl").write_text("live\n")
    (logs_folder / "minds-events.jsonl.20250101120000123456").write_text("rotated\n")
    (logs_folder / "minds.log").write_text("electron\n")

    uploader = ErrorAttachmentsS3Uploader(log_attachment_groups=_MINDS_LOG_ATTACHMENT_GROUPS)
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=logs_folder)

    assert set(groups) == {"", "live_logs", "rotated_logs", "electron_logs"}
    assert len(groups["live_logs"]) == 1
    assert len(groups["rotated_logs"]) == 1
    assert len(groups["electron_logs"]) == 1
    # one callback per upload: traceback + the three log files.
    assert len(callbacks) == 4
