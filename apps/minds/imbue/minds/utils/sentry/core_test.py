from pathlib import Path

import pytest

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.utils.sentry.core import ErrorAttachmentsS3Uploader
from imbue.minds.utils.sentry.core import MINDS_SENTRY_ENABLED_ENV_VAR
from imbue.minds.utils.sentry.core import SENTRY_DSN_DEV
from imbue.minds.utils.sentry.core import SENTRY_DSN_PRODUCTION
from imbue.minds.utils.sentry.core import SENTRY_DSN_STAGING
from imbue.minds.utils.sentry.core import SentryDeployEnvironment
from imbue.minds.utils.sentry.core import _SENTRY_DSN_BY_ENVIRONMENT
from imbue.minds.utils.sentry.core import is_sentry_enabled
from imbue.minds.utils.sentry.core import resolve_sentry_environment


def test_from_minds_env_name_maps_production_and_staging() -> None:
    assert SentryDeployEnvironment.from_minds_env_name("production") is SentryDeployEnvironment.PRODUCTION
    assert SentryDeployEnvironment.from_minds_env_name("staging") is SentryDeployEnvironment.STAGING


@pytest.mark.parametrize("env_name", ["dev-josh-1", "ci-ephemeral", "", "Production", "STAGING", None])
def test_from_minds_env_name_defaults_to_development(env_name: str | None) -> None:
    assert SentryDeployEnvironment.from_minds_env_name(env_name) is SentryDeployEnvironment.DEVELOPMENT


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


def test_dsn_map_pairs_each_environment_with_a_distinct_dsn() -> None:
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.PRODUCTION] == SENTRY_DSN_PRODUCTION
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.STAGING] == SENTRY_DSN_STAGING
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.DEVELOPMENT] == SENTRY_DSN_DEV
    assert len({SENTRY_DSN_PRODUCTION, SENTRY_DSN_STAGING, SENTRY_DSN_DEV}) == 3


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

    uploader = ErrorAttachmentsS3Uploader()
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


def test_collect_external_attachments_without_logs_folder_only_uploads_traceback() -> None:
    uploader = ErrorAttachmentsS3Uploader()
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=None)

    assert set(groups) == {""}
    assert len(callbacks) == 1
