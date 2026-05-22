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

Authentication piggy-backs on the same per-agent Bearer-token check
that gates the rest of ``/api/v1/...`` (see :mod:`api_key_auth`): an
ASGI wrapper extracts the ``Authorization: Bearer <api_key>`` header,
maps it to an :class:`AgentId` via
:func:`find_agent_by_api_key`, and 401s when the lookup fails. WsgiDAV
itself runs with anonymous auth -- the ASGI gate is the only thing
between the network and the filesystem.
"""

import tempfile
from pathlib import Path
from typing import Any
from typing import Final

from a2wsgi import WSGIMiddleware
from loguru import logger
from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import find_agent_by_api_key

_BEARER_PREFIX: Final[str] = "Bearer "
_AUTHORIZATION_HEADER: Final[bytes] = b"authorization"
_UNAUTHORIZED_BODY: Final[bytes] = b'{"error": "Unauthorized"}'


def _extract_bearer_token(scope: Scope) -> str | None:
    """Return the Bearer token from ``Authorization`` or ``None`` if absent / malformed."""
    for name, value in scope.get("headers", ()):
        if name == _AUTHORIZATION_HEADER:
            try:
                decoded = value.decode("latin-1")
            except UnicodeDecodeError:
                return None
            if not decoded.startswith(_BEARER_PREFIX):
                return None
            token = decoded[len(_BEARER_PREFIX) :]
            return token or None
    return None


async def _send_unauthorized(send: Send) -> None:
    """Emit a minimal 401 response with a ``WWW-Authenticate: Bearer`` challenge."""
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY, "more_body": False})


def _build_bearer_auth_gate(inner: ASGIApp, paths: WorkspacePaths) -> ASGIApp:
    """Wrap ``inner`` so every HTTP request must carry a valid ``Bearer <api_key>``."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await _send_unauthorized(send)
            return
        token = _extract_bearer_token(scope)
        if token is None:
            await _send_unauthorized(send)
            return
        if find_agent_by_api_key(paths.data_dir, token) is None:
            await _send_unauthorized(send)
            return
        await inner(scope, receive, send)

    return app


def _build_wsgidav_config(share_roots: tuple[Path, ...]) -> dict[str, Any]:
    """Build the WsgiDAV config dict for ``share_roots``."""
    provider_mapping: dict[str, FilesystemProvider] = {}
    for root in share_roots:
        provider_mapping[str(root)] = FilesystemProvider(str(root), readonly=False)
    return {
        "provider_mapping": provider_mapping,
        # Auth is enforced by the outer ASGI bearer-token gate; WsgiDAV
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


def create_webdav_app(paths: WorkspacePaths) -> ASGIApp:
    """Build the ASGI app to mount under ``/api/v1/files``.

    The returned callable serves ``Path.home()`` and ``tempfile.gettempdir()`` (typically /tmp) via
    WebDAV, gated by a per-agent Bearer token.
    """
    home_root = Path.home()
    tmp_root = Path(tempfile.gettempdir())
    share_roots = (home_root, tmp_root)
    config = _build_wsgidav_config(share_roots)
    wsgi_app = WsgiDAVApp(config)
    # ``a2wsgi.WSGIMiddleware`` is structurally an ASGI app, but it is
    # typed against its own ``a2wsgi.asgi_typing`` TypedDict aliases
    # which are nominally distinct from Starlette's looser
    # ``MutableMapping[str, Any]`` ASGI types. The ignore is the
    # project-preferred way (per PREVENT_CAST_USAGE) to express that.
    asgi_wsgi_app: ASGIApp = WSGIMiddleware(wsgi_app)  # ty: ignore[invalid-assignment]
    logger.debug(
        "Mounted WebDAV file server with shares: {}",
        ", ".join(str(root) for root in share_roots),
    )
    return _build_bearer_auth_gate(asgi_wsgi_app, paths)
