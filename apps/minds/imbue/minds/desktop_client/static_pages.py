"""Dependency-free static HTML documents for the few non-SPA responses.

The SPA owns every hub page; what remains server-rendered is the tiny set of
documents that must work before (or without) the SPA bundle: the one-time-code
login flow (:mod:`imbue.minds.desktop_client.ui_login`) and the error pages
below. All of them share one inline stylesheet so they read as the same app.
"""

from html import escape
from typing import Final

_PAGE_STYLE: Final[str] = (
    "body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;"
    "min-height:100vh;margin:0;background:#f7f7f5;color:#1a1a1a}"
    "main{max-width:26rem;padding:2rem;text-align:center}"
    "h1{font-size:1.25rem;margin-bottom:.75rem}"
    "p{color:#555;line-height:1.5}"
    "code{background:#ececec;border-radius:4px;padding:.1rem .35rem}"
    "a{color:#1a56db}"
    "button{font:inherit;padding:.35rem .9rem;border-radius:6px;border:1px solid #ccc;background:#fff;cursor:pointer}"
    "details{margin-top:1rem;text-align:left;color:#555}"
    "pre{white-space:pre-wrap;word-break:break-word;background:#ececec;border-radius:4px;padding:.5rem}"
)


def build_static_page_html(body_html: str, head_extra: str = "") -> str:
    """Wrap trusted body markup in the shared minimal document shell.

    ``body_html`` and ``head_extra`` are trusted fragments authored by the
    caller; any user-controlled text inside them must already be escaped.
    """
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        f'<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Imbue Studio</title><style>{_PAGE_STYLE}</style>{head_extra}</head>\n"
        f"<body><main>{body_html}</main></body>\n"
        "</html>\n"
    )


def build_error_page_html(
    title: str,
    message: str,
    details: str | None = None,
    is_retry_offered: bool = False,
    is_report_offered: bool = False,
) -> str:
    """A friendly full-page error document with a way back home.

    ``details`` (the underlying error, for whoever reports it) is shown only
    behind a collapsed "See details".
    """
    retry_html = '<button type="button" onclick="location.reload()">Try again</button> ' if is_retry_offered else ""
    report_html = ' &middot; <a href="/help">Report a bug</a>' if is_report_offered else ""
    details_html = f"<details><summary>See details</summary><pre>{escape(details)}</pre></details>" if details else ""
    return build_static_page_html(
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p>"
        f'<p>{retry_html}<a href="/">Back to workspaces</a>{report_html}</p>{details_html}'
    )
