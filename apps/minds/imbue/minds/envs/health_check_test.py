"""Unit tests for the post-deploy health-check poller.

Uses ``httpx.MockTransport`` to inject controlled HTTP responses --
no monkeypatching, no real network calls.
"""

from collections.abc import Callable

import httpx
import pytest
from pydantic import AnyUrl

from imbue.minds.envs.health_check import HealthCheckFailedError
from imbue.minds.envs.health_check import _is_transient_status
from imbue.minds.envs.health_check import await_apps_healthy
from imbue.minds.envs.health_check import check_once
from imbue.minds.errors import MindError


def test_is_transient_status_504_empty_body_is_transient() -> None:
    """Gateway timeouts with empty body always count as transient."""
    assert _is_transient_status(status_code=504, body_is_empty=True, elapsed_seconds=20.0) is True


def test_is_transient_status_500_during_cold_boot_is_transient() -> None:
    """Any 5xx within the cold-boot window is treated as transient."""
    assert _is_transient_status(status_code=500, body_is_empty=False, elapsed_seconds=2.0) is True


def test_is_transient_status_500_after_cold_boot_is_definitive() -> None:
    """5xx with a non-empty body AFTER cold-boot window is definitive."""
    assert _is_transient_status(status_code=500, body_is_empty=False, elapsed_seconds=20.0) is False


def test_is_transient_status_4xx_during_cold_boot_is_transient() -> None:
    """4xx within the cold-boot window is transient.

    Modal can serve a stale container from the prior version during the
    swap window (with ``min_containers=0``); requests can hit the old
    code's FastAPI app and 404 on routes that didn't exist there. We
    retry until the new container takes over.
    """
    assert _is_transient_status(status_code=404, body_is_empty=False, elapsed_seconds=2.0) is True


def test_is_transient_status_4xx_after_cold_boot_is_definitive() -> None:
    """4xx after the cold-boot window means the route is really missing -- definitive."""
    assert _is_transient_status(status_code=404, body_is_empty=False, elapsed_seconds=20.0) is False


def test_health_check_failed_error_is_a_minderror() -> None:
    """Subclass plumbing -- CLI's catch-MindError-and-suggest-recover works."""
    exc = HealthCheckFailedError("test")
    assert isinstance(exc, MindError)


def _client_with_response(status: int, body: str = "") -> httpx.Client:
    """Build an httpx.Client whose every request returns the given response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_once_4xx_during_cold_boot_is_transient() -> None:
    """During the cold-boot window an HTTP 4xx is transient (keep polling)."""
    with _client_with_response(404, "not found") as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring=None,
            per_attempt_timeout=1.0,
            elapsed_seconds=0.0,
        )
    assert ok is False
    assert reason is None


def test_check_once_4xx_after_cold_boot_is_definitive_failure() -> None:
    """After the cold-boot window an HTTP 4xx surfaces as a definitive failure."""
    with _client_with_response(404, "not found") as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring=None,
            per_attempt_timeout=1.0,
            elapsed_seconds=30.0,
        )
    assert ok is False
    assert reason is not None
    assert "404" in reason


def test_check_once_200_with_matching_substring_succeeds() -> None:
    with _client_with_response(200, '{"ok": true}') as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring="ok",
            per_attempt_timeout=1.0,
            elapsed_seconds=0.0,
        )
    assert ok is True
    assert reason is None


def test_check_once_200_with_missing_substring_is_definitive() -> None:
    """An HTTP 200 that doesn't contain the expected substring is definitive."""
    with _client_with_response(200, "wrong shape") as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring="ok",
            per_attempt_timeout=1.0,
            elapsed_seconds=0.0,
        )
    assert ok is False
    assert reason is not None
    assert "expected substring" in reason


def test_check_once_5xx_during_cold_boot_is_transient() -> None:
    """5xx during the cold-boot window returns (False, None) so the poller retries."""
    with _client_with_response(500, "still booting") as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring=None,
            per_attempt_timeout=1.0,
            elapsed_seconds=2.0,
        )
    assert ok is False
    assert reason is None


def test_check_once_5xx_after_cold_boot_with_body_is_definitive() -> None:
    """5xx after cold-boot with a body means the app is broken -- definitive."""
    with _client_with_response(500, "internal server error: kaboom") as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring=None,
            per_attempt_timeout=1.0,
            elapsed_seconds=20.0,
        )
    assert ok is False
    assert reason is not None
    assert "after cold-boot window" in reason


def _client_raising(exc: httpx.HTTPError) -> httpx.Client:
    """Build an httpx.Client whose every request raises ``exc`` at the transport layer."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_once_connect_error_is_transient() -> None:
    """A connection error (app still cold-booting / not yet routable) is transient."""
    with _client_raising(httpx.ConnectError("connection refused")) as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring=None,
            per_attempt_timeout=1.0,
            elapsed_seconds=0.0,
        )
    assert ok is False
    assert reason is None


def test_check_once_timeout_is_transient() -> None:
    """A socket timeout is transient -- the poller keeps trying within its budget."""
    with _client_raising(httpx.TimeoutException("slow")) as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring=None,
            per_attempt_timeout=1.0,
            elapsed_seconds=0.0,
        )
    assert ok is False
    assert reason is None


def test_check_once_other_httpx_error_is_definitive() -> None:
    """A non-timeout, non-connect httpx error surfaces as a definitive failure."""
    with _client_raising(httpx.HTTPError("kaboom")) as client:
        ok, reason = check_once(
            client=client,
            url="https://example.com",
            expected_substring=None,
            per_attempt_timeout=1.0,
            elapsed_seconds=0.0,
        )
    assert ok is False
    assert reason is not None
    assert "httpx error" in reason


def _factory_always(status: int, body: str = "") -> Callable[[], httpx.Client]:
    """A ``client_factory`` whose client always returns the given response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status, text=body)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


_CONNECTOR_URL = AnyUrl("https://connector.example")
_LITELLM_URL = AnyUrl("https://litellm.example")


def test_await_apps_healthy_returns_none_when_both_endpoints_healthy() -> None:
    """Both endpoints answer 200 immediately -> the poller returns without raising."""
    result = await_apps_healthy(
        connector_url=_CONNECTOR_URL,
        litellm_proxy_url=_LITELLM_URL,
        max_seconds=1.0,
        poll_interval=0.05,
        per_attempt_timeout=1.0,
        client_factory=_factory_always(200, '{"ok": true}'),
    )
    assert result is None


def test_await_apps_healthy_raises_on_definitive_failure() -> None:
    """A definitive failure (a 3xx redirect, e.g. a SuperTokens login page) fails fast.

    A redirect is definitive regardless of the cold-boot window, so the poller
    short-circuits to a HealthCheckFailedError instead of burning its budget.
    """
    with pytest.raises(HealthCheckFailedError, match="failed definitively"):
        await_apps_healthy(
            connector_url=_CONNECTOR_URL,
            litellm_proxy_url=_LITELLM_URL,
            max_seconds=1.0,
            poll_interval=0.05,
            per_attempt_timeout=1.0,
            client_factory=_factory_always(302, "redirecting to login"),
        )


def test_await_apps_healthy_raises_on_timeout_when_never_healthy() -> None:
    """A persistently-transient endpoint (503, empty body) exhausts the budget and fails."""
    with pytest.raises(HealthCheckFailedError, match="did not return 200"):
        await_apps_healthy(
            connector_url=_CONNECTOR_URL,
            litellm_proxy_url=_LITELLM_URL,
            max_seconds=0.2,
            poll_interval=0.05,
            per_attempt_timeout=1.0,
            client_factory=_factory_always(503, ""),
        )
