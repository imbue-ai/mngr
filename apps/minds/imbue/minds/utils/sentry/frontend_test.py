import pytest

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.utils.sentry.frontend import FrontendSentryConfig
from imbue.minds.utils.sentry.frontend import frontend_sentry_browser_payload
from imbue.minds.utils.sentry.frontend import frontend_sentry_ingest_origins
from imbue.minds.utils.sentry.frontend import resolve_frontend_sentry_config


def test_frontend_sentry_ingest_origins_are_https_scheme_only() -> None:
    origins = frontend_sentry_ingest_origins()
    # Every configured DSN yields at least one origin, and each is a bare
    # https://<host> with no key, path, or trailing slash (CSP connect-src form).
    assert origins
    for origin in origins:
        assert origin.startswith("https://")
        assert origin.count("/") == 2
        assert "@" not in origin


def test_to_browser_payload_is_none_when_disabled() -> None:
    config = FrontendSentryConfig(
        is_enabled=False,
        dsn="https://key@o1.ingest.us.sentry.io/2",
        environment="production",
        release="0.3.2",
        git_sha="abc1234",
        anonymous_user_id="0123456789abcdef0123456789abcdef",
    )
    assert config.to_browser_payload() is None


def test_to_browser_payload_is_none_without_dsn() -> None:
    config = FrontendSentryConfig(
        is_enabled=True,
        dsn=None,
        environment="production",
        release="0.3.2",
        git_sha="abc1234",
        anonymous_user_id="0123456789abcdef0123456789abcdef",
    )
    assert config.to_browser_payload() is None


def test_to_browser_payload_carries_dsn_environment_release_git_sha_and_user_id() -> None:
    config = FrontendSentryConfig(
        is_enabled=True,
        dsn="https://key@o1.ingest.us.sentry.io/2",
        environment="staging",
        release="0.3.2",
        git_sha="abc1234",
        anonymous_user_id="0123456789abcdef0123456789abcdef",
    )
    assert config.to_browser_payload() == {
        "dsn": "https://key@o1.ingest.us.sentry.io/2",
        "environment": "staging",
        "release": "0.3.2",
        "git_sha": "abc1234",
        "anonymous_user_id": "0123456789abcdef0123456789abcdef",
    }


def test_resolve_is_disabled_when_user_setting_off() -> None:
    assert (
        resolve_frontend_sentry_config(
            is_error_reporting_enabled=False, anonymous_user_id="0123456789abcdef0123456789abcdef"
        ).is_enabled
        is False
    )


def test_resolve_is_enabled_when_user_setting_on() -> None:
    assert (
        resolve_frontend_sentry_config(
            is_error_reporting_enabled=True, anonymous_user_id="0123456789abcdef0123456789abcdef"
        ).is_enabled
        is True
    )


def test_resolve_carries_anonymous_user_id() -> None:
    config = resolve_frontend_sentry_config(
        is_error_reporting_enabled=True, anonymous_user_id="0123456789abcdef0123456789abcdef"
    )
    assert config.anonymous_user_id == "0123456789abcdef0123456789abcdef"


def test_browser_payload_follows_user_setting() -> None:
    # The user's report_unexpected_errors setting alone decides whether the page emits a Sentry
    # bootstrap (the env DSNs are real, so the payload is present iff reporting is enabled).
    assert (
        frontend_sentry_browser_payload(
            is_error_reporting_enabled=False, anonymous_user_id="0123456789abcdef0123456789abcdef"
        )
        is None
    )
    assert (
        frontend_sentry_browser_payload(
            is_error_reporting_enabled=True, anonymous_user_id="0123456789abcdef0123456789abcdef"
        )
        is not None
    )


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
    assert (
        resolve_frontend_sentry_config(
            is_error_reporting_enabled=True, anonymous_user_id="0123456789abcdef0123456789abcdef"
        ).environment
        == expected_environment
    )
