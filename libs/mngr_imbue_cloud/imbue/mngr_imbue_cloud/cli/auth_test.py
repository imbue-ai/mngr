"""Tests for ``mngr imbue_cloud auth`` helpers.

Covers the OAuth localhost callback listener's handler. The handler must:
- Capture query params from a real ``GET /oauth/callback?...`` hit.
- NOT overwrite a previously-captured callback when secondary browser GETs
  (favicon, prefetches, service-worker pings) arrive at the same listener
  with no query params. Before the fix, those secondary GETs erased the
  captured params and the CLI then hung until the 300s OAuth timeout.

Also covers the CSRF ``state`` verification the CLI runs before exchanging the
OAuth code.

The ``running_callback_server`` fixture lives in ``cli/conftest.py``.
"""

import urllib.parse
import urllib.request

import pytest

from imbue.mngr_imbue_cloud.cli.auth import _OAuthCaptureBox
from imbue.mngr_imbue_cloud.cli.auth import _authorize_url_with_state
from imbue.mngr_imbue_cloud.cli.auth import _verify_oauth_callback_state


def _get(port: int, path: str) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5.0) as resp:
        return resp.status


def test_callback_handler_captures_oauth_query_params(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    box, port = running_callback_server
    status = _get(port, "/oauth/callback?code=abc123&state=xyz")
    assert status == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_followup_favicon_get(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    """Browsers fire a secondary GET /favicon.ico after the callback page renders.

    Before the fix this overwrote the captured params with ``{}``, causing the
    CLI's polling loop to never observe a truthy box and hang until timeout.
    """
    box, port = running_callback_server
    assert _get(port, "/oauth/callback?code=abc123&state=xyz") == 200
    assert _get(port, "/favicon.ico") == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_paramless_root_get(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    """A bare GET / (e.g. from a manual probe or prefetch) must not clobber the box."""
    box, port = running_callback_server
    assert _get(port, "/oauth/callback?code=abc123&state=xyz") == 200
    assert _get(port, "/") == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_query_params_on_wrong_path(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    """Even if some other path carries query params, only /oauth/callback should be captured."""
    box, port = running_callback_server
    assert _get(port, "/some-other-path?code=should_be_ignored") == 200
    assert box.get() is None


def test_verify_oauth_callback_state_accepts_matching_state() -> None:
    """A callback that echoes the exact state proceeds without raising."""
    _verify_oauth_callback_state("expected-state", {"code": "abc", "state": "expected-state"})


def test_verify_oauth_callback_state_rejects_mismatched_state() -> None:
    """A forged/replayed callback with the wrong state aborts before the code is exchanged."""
    with pytest.raises(SystemExit):
        _verify_oauth_callback_state("expected-state", {"code": "abc", "state": "forged"})


def test_verify_oauth_callback_state_rejects_missing_state() -> None:
    """A callback with no state at all is rejected (never trust a stateless callback)."""
    with pytest.raises(SystemExit):
        _verify_oauth_callback_state("expected-state", {"code": "abc"})


def test_verify_oauth_callback_state_rejects_non_ascii_state() -> None:
    """A non-ASCII forged state is a clean mismatch, not an uncaught TypeError from compare_digest."""
    with pytest.raises(SystemExit):
        _verify_oauth_callback_state("expected-state", {"code": "abc", "state": "stäte"})


def test_authorize_url_with_state_injects_state_client_side() -> None:
    """The CLI injects its own CSRF state into the authorize URL, replacing any existing one, exactly once."""
    result = _authorize_url_with_state(
        "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=old&scope=email",
        "my-csrf-state",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(result).query)
    assert query["state"] == ["my-csrf-state"]
    # Other params are preserved and PKCE-style challenges would be untouched.
    assert query["client_id"] == ["x"]
    assert query["scope"] == ["email"]
