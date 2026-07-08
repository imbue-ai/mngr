"""Unit tests for the /api/v1/files WebDAV mount."""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from flask.testing import FlaskClient
from wsgidav.wsgidav_app import WsgiDAVApp

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.webdav import _build_wsgidav_config


def _build_authenticated_client(tmp_path: Path) -> tuple[FlaskClient, str]:
    """Build a Flask test client + the central minds API key it expects."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    api_key = generate_api_key()
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
        minds_api_key=api_key,
    )
    return app.test_client(), api_key


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# Path used for "outside the home + /tmp shares" tests. ``/etc`` is
# always present on macOS and Linux, never readable as part of the
# WebDAV mount, and not a parent of either share root.
_OUTSIDE_SHARE_PATH = "/api/v1/files/etc/hostname"


# -- Auth --


def test_get_rejects_missing_auth(tmp_path: Path) -> None:
    client, _api_key = _build_authenticated_client(tmp_path)
    response = client.get(f"/api/v1/files{tmp_path}")
    assert response.status_code == 401


def test_get_rejects_invalid_bearer(tmp_path: Path) -> None:
    client, _api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        f"/api/v1/files{tmp_path}",
        headers={"Authorization": "Bearer not-a-real-key"},
    )
    assert response.status_code == 401


def test_get_rejects_non_bearer_scheme(tmp_path: Path) -> None:
    client, _api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        f"/api/v1/files{tmp_path}",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401


def test_propfind_rejects_missing_auth(tmp_path: Path) -> None:
    client, _api_key = _build_authenticated_client(tmp_path)
    response = client.open("/api/v1/files/tmp", method="PROPFIND", headers={"Depth": "0"})
    assert response.status_code == 401


# -- Share roots --

# The tmp share is the only one exercised here; the home share uses the
# same provider class and the same auth wrapper, so the tmp tests give
# us full coverage of the wiring without monkeypatching ``Path.home``.
#
# The tmp share root is ``tempfile.gettempdir()`` (what ``webdav.py``
# mounts), NOT a hardcoded ``/tmp``: under a ``TMPDIR`` override (e.g. the
# sandboxed test runner) the two differ, and a hardcoded ``/tmp`` path
# would not match the mounted provider and would 404. Deriving the test
# paths from the same call the app uses keeps these correct in every
# environment. Each file uses a unique name so parallel workers don't
# collide in the shared tmp dir.


def _tmp_share_root() -> Path:
    return Path(tempfile.gettempdir())


def _unique_tmp_file(suffix: str) -> Path:
    return _tmp_share_root() / f"webdav-unit-test-{suffix}-{uuid4().hex}.txt"


def test_get_serves_file_under_tmp(tmp_path: Path) -> None:
    """A file under the tmp share is reachable at /api/v1/files<path>."""
    client, api_key = _build_authenticated_client(tmp_path)
    target = _unique_tmp_file("get")
    target.write_bytes(b"hello via webdav")
    try:
        response = client.get(f"/api/v1/files{target}", headers=_auth_headers(api_key))
        assert response.status_code == 200
        assert response.data == b"hello via webdav"
    finally:
        target.unlink(missing_ok=True)


def test_propfind_on_tmp_returns_multistatus(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.open(
        f"/api/v1/files{_tmp_share_root()}",
        method="PROPFIND",
        headers={"Depth": "0", **_auth_headers(api_key)},
    )
    assert response.status_code == 207
    assert b"<ns0:multistatus" in response.data


def test_put_creates_file_under_tmp(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = _unique_tmp_file("put")
    target.unlink(missing_ok=True)
    try:
        response = client.put(
            f"/api/v1/files{target}",
            headers=_auth_headers(api_key),
            data=b"created via PUT",
        )
        assert response.status_code in (200, 201, 204)
        assert target.read_bytes() == b"created via PUT"
    finally:
        target.unlink(missing_ok=True)


def test_put_overwrites_existing_file(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = _unique_tmp_file("overwrite")
    target.write_bytes(b"original")
    try:
        response = client.put(
            f"/api/v1/files{target}",
            headers=_auth_headers(api_key),
            data=b"replacement",
        )
        assert response.status_code in (200, 201, 204)
        assert target.read_bytes() == b"replacement"
    finally:
        target.unlink(missing_ok=True)


def test_delete_removes_file_under_tmp(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = _unique_tmp_file("delete")
    target.write_bytes(b"goodbye")
    response = client.delete(
        f"/api/v1/files{target}",
        headers=_auth_headers(api_key),
    )
    assert response.status_code in (200, 204)
    assert not target.exists()


def test_share_root_with_uppercase_chars_resolves_to_provider(tmp_path: Path) -> None:
    """A share root containing uppercase chars (e.g. macOS ``/Users/<name>``) resolves.

    WsgiDAV lowercases share keys for matching but looks the matched share
    back up by that lowercased string. Without the lowercased-key
    workaround in ``_build_wsgidav_config`` a share like ``/Users/glenn``
    resolves to ``provider=None``, and a PROPFIND under it 404s with
    "Could not find resource provider". This reproduces that routing on
    any OS, so it fails before the fix regardless of the host home path.
    """
    # ``FilesystemProvider`` requires the share root to exist, so we make a
    # real directory whose path contains uppercase characters (mirroring a
    # macOS ``/Users/<Name>`` home).
    uppercase_root = tmp_path / "Users" / "Glenn"
    uppercase_root.mkdir(parents=True)
    config = _build_wsgidav_config((uppercase_root,))
    app = WsgiDAVApp(config)
    share, provider = app.resolve_provider(f"{uppercase_root}/Documents/Minds/notes.txt")
    assert provider is not None, f"share {share!r} did not resolve to a provider"


def test_paths_outside_share_roots_return_404(tmp_path: Path) -> None:
    """Paths outside ``Path.home()`` and ``/tmp`` are not served."""
    if not os.path.exists("/etc/hostname"):
        # Defensive: skip on hosts without the canonical /etc layout. The
        # share-root constraint is what's under test, not the existence
        # of any particular file.
        return
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(_OUTSIDE_SHARE_PATH, headers=_auth_headers(api_key))
    # WsgiDAV returns 404 when no provider matches the URL prefix.
    assert response.status_code == 404
