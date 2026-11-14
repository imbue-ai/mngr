"""The one-time-code login page as a dependency-free static HTML document.

Replaces the JinjaX ``LoginRedirect`` page (the SPA owns every other page).
The flow it preserves exactly:

- ``GET /login`` for an already-authenticated session redirects to ``/``.
- ``GET /login?one_time_code=<code>`` JS-redirects to
  ``/authenticate?one_time_code=<code>``. The JS hop is deliberate:
  link-preloading servers and prefetchers fetch ``/login`` but do not execute
  scripts, so a prefetch cannot consume the one-time code. ``/authenticate``
  (unchanged, in app.py) validates and consumes the code and sets the session
  cookie.
- A missing code renders a static explanation. In the desktop app's window
  (where the Electron preload exposes ``window.mindsNative``) it is the
  lapsed-login state, "Imbue Studio needs to restart to reconnect this
  window.", whose button restarts the app (which signs the window in again);
  anywhere else, Imbue Studio was started from a terminal, and the page points
  at the login URL printed there.

This is the *local session* with the desktop client itself; Imbue *account*
sign-in happens on the connector's hosted accounts pages in the system browser
(see ``supertokens_routes.py``).
"""

import json
from typing import Final

from flask import Response
from flask import request

from imbue.minds.desktop_client.static_pages import build_static_page_html
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated


def _render_login_document(body_html: str, head_extra: str = "") -> Response:
    return Response(build_static_page_html(body_html, head_extra=head_extra), mimetype="text/html")


_RESTART_BUTTON_STYLE: Final[str] = (
    "font:inherit;padding:.5rem 1rem;border-radius:6px;border:0;background:#1a1a1a;color:#fff;cursor:pointer"
)

# Swaps in the restart state inside the desktop app's window, whose preload
# exposes the restart call; a plain browser keeps the terminal instructions.
_SHOW_RESTART_IN_DESKTOP_WINDOW_SCRIPT: Final[str] = (
    "<script>(function(){"
    "var native=window.mindsNative;"
    'if(!native||typeof native.retry!=="function")return;'
    'document.getElementById("login-terminal").hidden=true;'
    'document.getElementById("login-restart").hidden=false;'
    'document.getElementById("login-restart-button").addEventListener("click",function(){'
    "this.disabled=true;native.retry();});"
    "})();</script>"
)


def render_missing_code_page() -> Response:
    return _render_login_document(
        '<div id="login-terminal">'
        "<h1>Sign in to Imbue Studio</h1>"
        "<p>This page needs the one-time login link printed in your terminal.</p>"
        "<p>Find the line with the login URL where the Imbue Studio app is running and open "
        "that full link here.</p>"
        "</div>"
        '<div id="login-restart" hidden>'
        "<h1>Reconnect this window</h1>"
        "<p>Imbue Studio needs to restart to reconnect this window.</p>"
        f'<p><button type="button" id="login-restart-button" style="{_RESTART_BUTTON_STYLE}">'
        "Restart Imbue Studio</button></p>"
        "</div>" + _SHOW_RESTART_IN_DESKTOP_WINDOW_SCRIPT
    )


def render_login_redirect_response(one_time_code: str) -> Response:
    """The JS redirect hop to /authenticate that keeps prefetchers from consuming the code."""
    # json.dumps produces a valid JS string literal for the code; "</" cannot
    # appear (the charset is checked by /authenticate, and escaping here keeps
    # even a hostile value from breaking out of the script tag).
    code_js_literal = json.dumps(one_time_code).replace("</", "<\\/")
    redirect_script = (
        "<script>window.location.replace('/authenticate?one_time_code=' "
        f"+ encodeURIComponent({code_js_literal}));</script>"
    )
    return _render_login_document(
        "<h1>Signing you in&hellip;</h1><p>One moment.</p>",
        head_extra=redirect_script,
    )


def handle_static_login_page() -> Response:
    """GET /login: JS-redirect a provided code to /authenticate, explain otherwise."""
    if is_ui_request_authenticated():
        return Response(status=307, headers={"Location": "/"})
    raw_code = request.args.get("one_time_code", "")
    if not raw_code:
        return render_missing_code_page()
    return render_login_redirect_response(raw_code)
