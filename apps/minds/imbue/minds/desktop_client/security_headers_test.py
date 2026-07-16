"""Unit tests for the first-party security response headers.

The desktop client proxies semi-trusted per-agent content on
``<agent-id>.localhost`` subdomains, so the restrictive CSP must apply only to
the first-party chrome origin -- clamping it onto proxied agent responses would
break agent web apps.
"""

from flask import Response

from imbue.minds.desktop_client.app import _FIRST_PARTY_CSP
from imbue.minds.desktop_client.app import _apply_security_headers
from imbue.minds.desktop_client.app import _is_first_party_host
from imbue.minds.utils.sentry.frontend import frontend_sentry_ingest_origins


def test_is_first_party_host_accepts_bare_localhost() -> None:
    assert _is_first_party_host("localhost:8888") is True
    assert _is_first_party_host("localhost") is True
    assert _is_first_party_host("127.0.0.1:8888") is True


def test_is_first_party_host_rejects_agent_subdomain() -> None:
    assert _is_first_party_host("agent-abc123.localhost:8888") is False


def test_is_first_party_host_defaults_true_when_absent() -> None:
    assert _is_first_party_host(None) is True
    assert _is_first_party_host("") is True


def test_first_party_response_carries_nosniff_and_csp() -> None:
    response = _apply_security_headers(Response(), "localhost:8888")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == _FIRST_PARTY_CSP


def test_proxied_agent_response_does_not_get_restrictive_csp() -> None:
    response = _apply_security_headers(Response(), "agent-abc123.localhost:8888")

    # nosniff is harmless everywhere, but the restrictive CSP must NOT be
    # applied to proxied agent content (agents control their own CSP).
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in response.headers


def test_csp_connect_src_allows_sentry_ingest_and_loopback_websockets() -> None:
    # The browser Sentry SDK POSTs opt-in error events to the ingest origin; the
    # first-party CSP must permit it or reporting silently breaks. Websocket
    # streaming to the local desktop client / mngr-forward proxy must stay
    # reachable, while exfiltration to a non-loopback host stays blocked.
    # At least one ingest origin is always configured (the DSN table is non-empty).
    ingest_origins = frontend_sentry_ingest_origins()
    assert ingest_origins
    for origin in ingest_origins:
        assert origin in _FIRST_PARTY_CSP
    assert "ws://localhost:*" in _FIRST_PARTY_CSP
    assert "wss://127.0.0.1:*" in _FIRST_PARTY_CSP
    # Bare-scheme ``ws:``/``wss:`` (any host) would defeat the exfiltration guard.
    assert "connect-src 'self' ws: wss:" not in _FIRST_PARTY_CSP


def test_security_headers_do_not_clobber_handler_set_values() -> None:
    response = Response()
    response.headers["Content-Security-Policy"] = "default-src 'none'"
    response.headers["X-Content-Type-Options"] = "custom"

    _apply_security_headers(response, "localhost:8888")

    # ``setdefault`` semantics: a handler that set its own values wins.
    assert response.headers["Content-Security-Policy"] == "default-src 'none'"
    assert response.headers["X-Content-Type-Options"] == "custom"
