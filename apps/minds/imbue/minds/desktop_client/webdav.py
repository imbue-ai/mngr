"""WebDAV file server mounted under ``/api/v1/files``.

Backed by `wsgidav <https://wsgidav.readthedocs.io/>`.

Every path starts with this desktop's device id, so that the user's
desktops each serve their own files under a URL no other desktop answers
(a remote workspace's gateway can reach several of them). Anything under
another device id is refused with 404. Below the device id, two share
roots are exposed:

* the current user's home directory (``Path.home()``); and
* ``/tmp``.

Each share is mounted at its own absolute path so that the outward URL
mirrors the on-disk path one-to-one: on desktop ``host-abc``, a file at
``/home/<user>/foo.txt`` is reached via
``/api/v1/files/host-abc/home/<user>/foo.txt``, a file at
``/tmp/blob.bin`` via ``/api/v1/files/host-abc/tmp/blob.bin``. Paths
outside those two roots are not served.

The same files are also served without the device id
(``/api/v1/files/home/<user>/foo.txt``), which is where they lived before
desktops were told apart and where workspaces built then still look.

Authentication piggy-backs on the same central-key Bearer-token check
that gates the rest of ``/api/v1/...`` (see :mod:`api_key_auth`): a
WSGI wrapper extracts the ``Authorization: Bearer <key>`` header and
401s unless it matches ``get_state().minds_api_key``. WsgiDAV itself
runs with anonymous auth -- the WSGI gate is the only thing between
the network and the filesystem. WsgiDAV is already a WSGI app, so it is
mounted directly via Werkzeug's ``DispatcherMiddleware`` (no ASGI bridge).
"""

import json
import tempfile
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from wsgiref.types import StartResponse
from wsgiref.types import WSGIApplication
from wsgiref.types import WSGIEnvironment

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.api_key_auth import is_request_authenticated

# Callable that resolves the current central Imbue Studio API key. Wrapped so
# the WebDAV gate can look it up fresh on every request via
# ``get_state().minds_api_key`` instead of capturing a stale value at
# gate-build time.
ExpectedKeyProvider = Callable[[], str | None]

_UNAUTHORIZED_BODY: Final[bytes] = b'{"error": "Unauthorized"}'


def _build_bearer_auth_gate(inner: WSGIApplication, expected_key_provider: ExpectedKeyProvider) -> WSGIApplication:
    """Wrap ``inner`` so every request must carry the central Imbue Studio API key.

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


class _DeviceGate(FrozenModel):
    """WSGI middleware letting through only requests under ``/<device_id>``, with that segment moved onto the mount.

    WsgiDAV then serves the path below the device id. An empty ``device_id``
    (an app built without one) serves nothing under a device id.

    A path that starts with no device id but is under a share root is let
    through as it is: that is the URL workspaces built before desktops were
    told apart ask for.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: SkipValidation[WSGIApplication] = Field(
        frozen=True, description="The WebDAV app serving this desktop's files."
    )
    device_id: str = Field(frozen=True, description="This desktop's device id; empty when the app has none.")
    # CLEANUP: drop this field, the branch of ``__call__`` that reads it, ``_is_under_any_share_root``
    # and what this module's docstrings say about the device-less URL once no supported workspace
    # reaches shared files through that URL.
    share_roots: tuple[Path, ...] = Field(
        frozen=True, description="The roots the WebDAV app serves, which a device-less path has to be under."
    )

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        path = str(environ.get("PATH_INFO", ""))
        requested_device_id, separator, path_below_device = path.lstrip("/").partition("/")
        if self.device_id and requested_device_id == self.device_id:
            device_environ = {
                **environ,
                "SCRIPT_NAME": f"{environ.get('SCRIPT_NAME', '')}/{requested_device_id}",
                "PATH_INFO": f"/{path_below_device}" if separator else "",
            }
            return self.inner(device_environ, start_response)
        if _is_under_any_share_root(path, self.share_roots):
            return self.inner(environ, start_response)
        message = (
            f"This desktop is {self.device_id}; it only serves files under /api/v1/files/{self.device_id}/."
            if self.device_id
            else "File sharing is unavailable: this desktop has no device id."
        )
        body = json.dumps({"error": message}).encode()
        start_response(
            "404 Not Found",
            [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
        )
        return [body]


@pure
def _is_under_any_share_root(path: str, share_roots: Sequence[Path]) -> bool:
    """Whether ``path`` is one of ``share_roots`` or below one, compared the case-insensitive way WsgiDAV matches shares."""
    lowercased_path = path.lower()
    return any(
        lowercased_path == str(root).lower() or lowercased_path.startswith(f"{str(root).lower()}/")
        for root in share_roots
    )


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


def create_webdav_app(expected_key_provider: ExpectedKeyProvider, device_id: str) -> WSGIApplication:
    """Build the WSGI app to mount under ``/api/v1/files``.

    The returned callable serves ``Path.home()`` and ``tempfile.gettempdir()`` (typically /tmp) via
    WebDAV under ``/<device_id>`` (and, for workspaces from before desktops were told apart, directly
    under the mount), gated by the central minds-api Bearer token resolved through
    ``expected_key_provider`` on each request.
    """
    share_roots = get_file_sharing_roots()
    config = _build_wsgidav_config(share_roots)
    wsgi_app: WSGIApplication = WsgiDAVApp(config)
    logger.debug(
        "Mounted WebDAV file server with shares: {}",
        ", ".join(str(root) for root in share_roots),
    )
    return _build_bearer_auth_gate(
        _DeviceGate(inner=wsgi_app, device_id=device_id, share_roots=share_roots), expected_key_provider
    )
