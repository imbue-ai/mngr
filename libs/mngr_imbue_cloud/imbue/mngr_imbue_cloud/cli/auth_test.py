"""Tests for ``mngr imbue_cloud auth`` helpers.

Covers the OAuth localhost callback listener's handler. The handler must:
- Capture query params from a real ``GET /oauth/callback?...`` hit.
- NOT overwrite a previously-captured callback when secondary browser GETs
  (favicon, prefetches, service-worker pings) arrive at the same listener
  with no query params. Before the fix, those secondary GETs erased the
  captured params and the CLI then hung until the 300s OAuth timeout.

The ``running_callback_server`` fixture lives in ``cli/conftest.py``.
"""

import urllib.request

from imbue.mngr_imbue_cloud.cli.auth import _OAuthCaptureBox
from imbue.mngr_imbue_cloud.cli.auth import _oauth_success_page


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


def test_success_page_without_redirect_says_return_to_terminal() -> None:
    page = _oauth_success_page(None).decode("utf-8")
    assert "return to your terminal" in page
    assert "<script>" not in page


def test_success_page_with_redirect_links_to_url_without_auto_navigation() -> None:
    # Deliberately a plain link, not an automatic navigation: the click is the
    # user gesture that triggers the browser's open-external-app prompt. The
    # app-driven variant carries the minds wordmark and copy.
    page = _oauth_success_page("minds://").decode("utf-8")
    assert '<a href="minds://">Open app</a>' in page
    assert '<div class="wordmark">minds</div>' in page
    assert "Feel free to close this tab." in page
    assert "<script>" not in page


def test_success_page_escapes_redirect_url_markup() -> None:
    """A crafted URL must not be able to inject markup into the page: the
    href is attribute-escaped."""
    page = _oauth_success_page('minds://x?a=<b>&q="hi"').decode("utf-8")
    assert "<b>" not in page
    assert 'href="minds://x?a=&lt;b&gt;&amp;q=&quot;hi&quot;"' in page
