"""Flask response factories mirroring the FastAPI response constructors.

These keep the desktop client's many handler return sites terse and
behavior-compatible after the FastAPI -> Flask migration: ``make_response``
mirrors ``fastapi.Response`` (defaulting to ``text/plain``), ``make_html_response``
mirrors ``HTMLResponse``, etc. Streaming responses are wrapped in
``stream_with_context`` so the request/app context stays alive while the
generator is consumed (the SSE handlers read ``request`` / ``current_app``
mid-stream).
"""

from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path

from flask import Response
from flask import send_file
from flask import stream_with_context

_DEFAULT_MEDIA_TYPE = "text/plain"


def make_response(
    content: str | bytes = "",
    status_code: int = 200,
    media_type: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Build a Flask response, defaulting to ``text/plain`` like ``fastapi.Response``."""
    response = Response(response=content, status=status_code, mimetype=media_type or _DEFAULT_MEDIA_TYPE)
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response


def make_html_response(
    content: str = "",
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Build an ``text/html`` Flask response, mirroring ``HTMLResponse``."""
    return make_response(content=content, status_code=status_code, media_type="text/html", headers=headers)


def make_redirect_response(url: str, status_code: int = 307) -> Response:
    """Build a redirect response with an explicit status code, mirroring ``RedirectResponse``."""
    return make_response(content="", status_code=status_code, headers={"Location": url})


def safe_local_redirect_path(raw: str | None) -> str | None:
    """Return ``raw`` only if it is a safe same-origin redirect path, else ``None``.

    Guards the ``return_to`` flow (the create page -> sign-in -> back to the
    picker) against open redirects: a value is accepted only when it is a
    root-relative path (starts with a single ``/``) with no scheme or host.
    Protocol-relative URLs (``//evil.com`` and the backslash-form
    ``/\\evil.com``) and absolute ``https://...`` URLs are all rejected.
    """
    if not raw or not raw.startswith("/"):
        return None
    if raw.startswith("//") or raw.startswith("/\\"):
        return None
    return raw


def make_file_response(path: str | Path, media_type: str | None = None, filename: str | None = None) -> Response:
    """Stream a file as an attachment, mirroring ``FileResponse(path=..., filename=...)``."""
    return send_file(str(path), mimetype=media_type, as_attachment=filename is not None, download_name=filename)


def make_streaming_response(
    content: Iterator[str],
    media_type: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Build a streaming response from a generator, mirroring ``StreamingResponse``.

    The generator is wrapped in ``stream_with_context`` so it can read
    ``request`` / ``current_app`` while it is being consumed.
    """
    response = Response(stream_with_context(content), mimetype=media_type or _DEFAULT_MEDIA_TYPE)
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response
