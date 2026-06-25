import pytest

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.utils.sentry.core import MINDS_SENTRY_ENABLED_ENV_VAR
from imbue.minds.utils.sentry.frontend import FrontendSentryConfig
from imbue.minds.utils.sentry.frontend import resolve_frontend_sentry_config


def test_to_browser_payload_is_none_when_disabled() -> None:
    config = FrontendSentryConfig(
        is_enabled=False,
        dsn="https://key@o1.ingest.us.sentry.io/2",
        environment="production",
        release="0.3.2",
        git_sha="abc1234",
    )
    assert config.to_browser_payload() is None


def test_to_browser_payload_is_none_without_dsn() -> None:
    config = FrontendSentryConfig(
        is_enabled=True,
        dsn=None,
        environment="production",
        release="0.3.2",
        git_sha="abc1234",
    )
    assert config.to_browser_payload() is None


def test_to_browser_payload_carries_dsn_environment_release_and_git_sha() -> None:
    config = FrontendSentryConfig(
        is_enabled=True,
        dsn="https://key@o1.ingest.us.sentry.io/2",
        environment="staging",
        release="0.3.2",
        git_sha="abc1234",
    )
    assert config.to_browser_payload() == {
        "dsn": "https://key@o1.ingest.us.sentry.io/2",
        "environment": "staging",
        "release": "0.3.2",
        "git_sha": "abc1234",
    }


def test_resolve_is_disabled_when_opt_in_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINDS_SENTRY_ENABLED_ENV_VAR, raising=False)
    config = resolve_frontend_sentry_config()
    assert config.is_enabled is False


def test_resolve_tracks_opt_in_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MINDS_SENTRY_ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    config = resolve_frontend_sentry_config()
    assert config.is_enabled is True
    assert config.environment == "staging"


@pytest.mark.parametrize(
    ("root_name", "expected_environment"),
    [
        ("minds", "production"),
        ("minds-staging", "staging"),
        ("minds-dev-someone", "development"),
    ],
)
def test_resolve_environment_follows_activated_root_name(
    monkeypatch: pytest.MonkeyPatch, root_name: str, expected_environment: str
) -> None:
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, root_name)
    assert resolve_frontend_sentry_config().environment == expected_environment
