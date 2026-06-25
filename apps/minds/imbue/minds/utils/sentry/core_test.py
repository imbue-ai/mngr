from pathlib import Path

import pytest

from imbue.imbue_common.sentry.core import ErrorAttachmentsS3Uploader
from imbue.imbue_common.sentry.data_types import SentryDeployEnvironment
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.utils.sentry.core import MINDS_SENTRY_ENABLED_ENV_VAR
from imbue.minds.utils.sentry.core import MINDS_SENTRY_S3_UPLOADS_ENV_VAR
from imbue.minds.utils.sentry.core import _MINDS_LOG_ATTACHMENT_GROUPS
from imbue.minds.utils.sentry.core import is_sentry_enabled
from imbue.minds.utils.sentry.core import is_sentry_s3_upload_enabled
from imbue.minds.utils.sentry.core import resolve_latchkey_forward_sentry_env
from imbue.minds.utils.sentry.core import resolve_sentry_environment
from imbue.minds.utils.sentry.core import sentry_deploy_environment_from_minds_env_name
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR
from imbue.mngr_latchkey.sentry import resolve_forward_sentry_config


def test_sentry_environment_from_minds_env_name_maps_production_and_staging() -> None:
    assert sentry_deploy_environment_from_minds_env_name("production") is SentryDeployEnvironment.PRODUCTION
    assert sentry_deploy_environment_from_minds_env_name("staging") is SentryDeployEnvironment.STAGING


@pytest.mark.parametrize("env_name", ["dev-josh-1", "ci-ephemeral", "", "Production", "STAGING", None])
def test_sentry_environment_from_minds_env_name_defaults_to_development(env_name: str | None) -> None:
    assert sentry_deploy_environment_from_minds_env_name(env_name) is SentryDeployEnvironment.DEVELOPMENT


@pytest.mark.parametrize("raw_value", ["1", "true", "TRUE", "yes", " Yes "])
def test_is_sentry_enabled_accepts_truthy_values(monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
    monkeypatch.setenv(MINDS_SENTRY_ENABLED_ENV_VAR, raw_value)
    assert is_sentry_enabled() is True


@pytest.mark.parametrize("raw_value", ["0", "false", "no", ""])
def test_is_sentry_enabled_rejects_other_values(monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
    monkeypatch.setenv(MINDS_SENTRY_ENABLED_ENV_VAR, raw_value)
    assert is_sentry_enabled() is False


def test_is_sentry_enabled_defaults_to_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINDS_SENTRY_ENABLED_ENV_VAR, raising=False)
    assert is_sentry_enabled() is False


@pytest.mark.parametrize("raw_value", ["1", "true", " Yes "])
def test_is_sentry_s3_upload_enabled_accepts_truthy_values(monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
    monkeypatch.setenv(MINDS_SENTRY_S3_UPLOADS_ENV_VAR, raw_value)
    assert is_sentry_s3_upload_enabled() is True


def test_is_sentry_s3_upload_enabled_defaults_to_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINDS_SENTRY_S3_UPLOADS_ENV_VAR, raising=False)
    assert is_sentry_s3_upload_enabled() is False


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


def test_resolve_latchkey_forward_sentry_env_round_trips_into_forward_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # The env vars minds publishes for the daemon must be consumable by the daemon's
    # own resolver, yielding minds' opt-in + environment. Verified end-to-end here so
    # the two sides (publisher and consumer) cannot drift apart.
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    monkeypatch.setenv(MINDS_SENTRY_ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(MINDS_SENTRY_S3_UPLOADS_ENV_VAR, "1")
    published_env = resolve_latchkey_forward_sentry_env()
    assert published_env[MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR] == "1"
    assert published_env[MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR] == "1"
    assert published_env[MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR] == SentryDeployEnvironment.STAGING.value

    for env_var_name, value in published_env.items():
        monkeypatch.setenv(env_var_name, value)
    forward_config = resolve_forward_sentry_config()
    assert forward_config is not None
    assert forward_config.environment is SentryDeployEnvironment.STAGING
    assert forward_config.is_s3_upload_enabled is True


def test_resolve_latchkey_forward_sentry_env_disabled_when_minds_sentry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINDS_SENTRY_ENABLED_ENV_VAR, raising=False)
    published_env = resolve_latchkey_forward_sentry_env()
    assert published_env[MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR] == "0"
    for env_var_name, value in published_env.items():
        monkeypatch.setenv(env_var_name, value)
    assert resolve_forward_sentry_config() is None


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
