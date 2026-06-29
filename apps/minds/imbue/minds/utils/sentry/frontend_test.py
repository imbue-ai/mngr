import pytest

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.utils.sentry.frontend import FrontendSentryConfig
from imbue.minds.utils.sentry.frontend import frontend_sentry_browser_payload
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


def test_resolve_is_disabled_when_user_setting_off() -> None:
    assert resolve_frontend_sentry_config(is_error_reporting_enabled=False).is_enabled is False


def test_resolve_is_enabled_when_user_setting_on() -> None:
    assert resolve_frontend_sentry_config(is_error_reporting_enabled=True).is_enabled is True


def test_browser_payload_follows_user_setting() -> None:
    # The user's report_unexpected_errors setting alone decides whether the page emits a Sentry
    # bootstrap (the env DSNs are real, so the payload is present iff reporting is enabled).
    assert frontend_sentry_browser_payload(is_error_reporting_enabled=False) is None
    assert frontend_sentry_browser_payload(is_error_reporting_enabled=True) is not None


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
    assert resolve_frontend_sentry_config(is_error_reporting_enabled=True).environment == expected_environment
