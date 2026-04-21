"""Unit tests for the minds desktop client's supertokens_routes helpers.

End-to-end flow through the FastAPI app is covered by
``test_supertokens_auth_e2e.py``; these tests isolate the non-HTTP helpers
(OAuth CSRF state tracking, etc.) so they run fast and without external deps.
"""

from imbue.minds.desktop_client.supertokens_routes import _consume_oauth_state
from imbue.minds.desktop_client.supertokens_routes import _extract_state_from_auth_url
from imbue.minds.desktop_client.supertokens_routes import _remember_oauth_state


def test_extract_state_returns_state_when_present() -> None:
    url = "https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=X&state=abc123&redirect_uri=Y"
    assert _extract_state_from_auth_url(url) == "abc123"


def test_extract_state_returns_none_when_absent() -> None:
    url = "https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=X"
    assert _extract_state_from_auth_url(url) is None


def test_remember_then_consume_returns_state() -> None:
    _remember_oauth_state("google", "state-token-42")
    assert _consume_oauth_state("google") == "state-token-42"


def test_consume_is_single_use() -> None:
    """A consumed state is removed so a replay of the callback fails."""
    _remember_oauth_state("github", "state-replay-target")
    first = _consume_oauth_state("github")
    second = _consume_oauth_state("github")
    assert first == "state-replay-target"
    assert second is None


def test_consume_without_prior_remember_returns_none() -> None:
    assert _consume_oauth_state("provider-never-seen") is None


def test_remember_replaces_previous_state_for_same_provider() -> None:
    """Only the most recent OAuth attempt's state is remembered per provider."""
    _remember_oauth_state("google", "first")
    _remember_oauth_state("google", "second")
    assert _consume_oauth_state("google") == "second"
