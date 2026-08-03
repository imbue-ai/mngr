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
- A missing code renders a static explanation pointing the user at the login
  URL printed in the terminal.

The SuperTokens account-auth pages (sign-up/sign-in, OAuth close, forgot
password) are NOT part of this flow and still render from ``templates/auth/``;
the full auth overhaul is a separate project.
"""

import json
from typing import Final

from flask import Response
from flask import request

from imbue.minds.desktop_client.ui_api import is_ui_request_authenticated

_PAGE_STYLE: Final[str] = (
    "body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;"
    "min-height:100vh;margin:0;background:#f7f7f5;color:#1a1a1a}"
    "main{max-width:26rem;padding:2rem;text-align:center}"
    "h1{font-size:1.25rem;margin-bottom:.75rem}"
    "p{color:#555;line-height:1.5}"
    "code{background:#ececec;border-radius:4px;padding:.1rem .35rem}"
)


def _render_login_document(body_html: str, head_extra: str = "") -> Response:
    document = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        f'<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>minds</title><style>{_PAGE_STYLE}</style>{head_extra}</head>\n"
        f"<body><main>{body_html}</main></body>\n"
        "</html>\n"
    )
    return Response(document, mimetype="text/html")


def render_missing_code_page() -> Response:
    return _render_login_document(
        "<h1>Sign in to minds</h1>"
        "<p>This page needs the one-time login link printed in your terminal.</p>"
        "<p>Find the line with the login URL where the minds app is running and open "
        "that full link here.</p>"
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
