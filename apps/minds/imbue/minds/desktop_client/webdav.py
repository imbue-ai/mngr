"""WebDAV file server mounted under ``/api/v1/files``.

Backed by `wsgidav <https://wsgidav.readthedocs.io/>`.

Two share roots are exposed:

* the current user's home directory (``Path.home()``); and
* ``/tmp``.

Each share is mounted at its own absolute path so that the outward URL
mirrors the on-disk path one-to-one: a file at ``/home/<user>/foo.txt``
is reached via ``/api/v1/files/home/<user>/foo.txt``, a file at
``/tmp/blob.bin`` via ``/api/v1/files/tmp/blob.bin``. Paths outside
those two roots are not served.

Authentication piggy-backs on the same central-key Bearer-token check
that gates the rest of ``/api/v1/...`` (see :mod:`api_key_auth`): a
WSGI wrapper extracts the ``Authorization: Bearer <key>`` header and
401s unless it matches ``get_state().minds_api_key``. WsgiDAV itself
runs with anonymous auth -- the WSGI gate is the only thing between
the network and the filesystem. WsgiDAV is already a WSGI app, so it is
mounted directly via Werkzeug's ``DispatcherMiddleware`` (no ASGI bridge).
"""

import tempfile
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from typing import Final
from wsgiref.types import StartResponse
from wsgiref.types import WSGIApplication
from wsgiref.types import WSGIEnvironment

from loguru import logger
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from imbue.minds.desktop_client.api_key_auth import is_request_authenticated

# Callable that resolves the current central minds API key. Wrapped so
# the WebDAV gate can look it up fresh on every request via
# ``get_state().minds_api_key`` instead of capturing a stale value at
# gate-build time.
ExpectedKeyProvider = Callable[[], str | None]

_UNAUTHORIZED_BODY: Final[bytes] = b'{"error": "Unauthorized"}'


def _build_bearer_auth_gate(inner: WSGIApplication, expected_key_provider: ExpectedKeyProvider) -> WSGIApplication:
    """Wrap ``inner`` so every request must carry the central minds API key.

    ``expected_key_provider`` resolves the live ``get_state().minds_api_key``
    on each request rather than capturing the value at gate-build time;
    that way an empty / unset key fails closed and tests that construct
    the app without populating the state still see 401s rather than 500s.
    """

    def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        authorization = environ.get("HTTP_AUTHORIZATION")
        expected_key = expected_key_provider()
        if expected_key is None or not is_request_authenticated(authorization, expected_key):
            start_response(
                "401 Unauthorized",
                [
                    ("Content-Type", "application/json"),
                    ("WWW-Authenticate", "Bearer"),
                    ("Content-Length", str(len(_UNAUTHORIZED_BODY))),
                ],
            )
            return [_UNAUTHORIZED_BODY]
        return inner(environ, start_response)

    return app


def _build_wsgidav_config(share_roots: tuple[Path, ...]) -> dict[str, Any]:
    """Build the WsgiDAV config dict for ``share_roots``."""
    provider_mapping: dict[str, FilesystemProvider] = {}
    for root in share_roots:
        # WsgiDAV matches the request path against a *lowercased* copy of
        # the share keys but then looks the matched share back up in
        # ``provider_mapping`` using that lowercased string. A share key
        # containing uppercase characters (e.g. a macOS home directory
        # ``/Users/<name>``) therefore never resolves: the lookup misses,
        # the provider comes back ``None``, and WsgiDAV answers 404. We
        # register the share under a lowercased key so the lookup always
        # hits; the ``FilesystemProvider`` keeps the real, correct-case
        # path so files still resolve on case-sensitive filesystems. The
        # share prefix length is identical regardless of case, so WsgiDAV's
        # ``PATH_INFO`` stripping stays correct.
        provider_mapping[str(root).lower()] = FilesystemProvider(str(root), readonly=False)
    return {
        "provider_mapping": provider_mapping,
        # Auth is enforced by the outer WSGI bearer-token gate; WsgiDAV
        # itself accepts any caller (the gate guarantees no anonymous
        # caller ever reaches it).
        "simple_dc": {"user_mapping": {"*": True}},
        "http_authenticator": {
            "domain_controller": None,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
            "trusted_auth_header": None,
        },
        # WsgiDAV configures its own loggers when ``enable`` is true; we
        # let loguru own logging instead and keep WsgiDAV silent.
        "logging": {"enable_loggers": []},
        "verbose": 1,
        # The HTML directory-listing endpoint is unnecessary for the
        # programmatic file-sharing use case and just adds attack
        # surface.
        "dir_browser": {"enable": False},
    }


def get_file_sharing_roots() -> tuple[Path, ...]:
    """Return the on-disk roots the WebDAV file server mounts.

    Currently the current user's home directory and the system temp
    directory. This is the single source of truth for "which paths are
    shareable": the WebDAV mount is built from it, and the file-sharing
    permission handler validates a requested (or user-edited) path
    against it so a path outside these roots is rejected with a clear
    error before it ever reaches the gateway (which would otherwise be
    the only thing to catch it, and only as a less-friendly 4xx).
    """
    return (Path.home(), Path(tempfile.gettempdir()))


def create_webdav_app(expected_key_provider: ExpectedKeyProvider) -> WSGIApplication:
    """Build the WSGI app to mount under ``/api/v1/files``.

    The returned callable serves ``Path.home()`` and ``tempfile.gettempdir()`` (typically /tmp) via
    WebDAV, gated by the central minds-api Bearer token resolved through
    ``expected_key_provider`` on each request.
    """
    share_roots = get_file_sharing_roots()
    config = _build_wsgidav_config(share_roots)
    wsgi_app: WSGIApplication = WsgiDAVApp(config)
    logger.debug(
        "Mounted WebDAV file server with shares: {}",
        ", ".join(str(root) for root in share_roots),
    )
    return _build_bearer_auth_gate(wsgi_app, expected_key_provider)
