"""Unit tests for the /api/v1/file-server endpoints."""

import json
from pathlib import Path

from starlette.testclient import TestClient

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import hash_api_key
from imbue.minds.desktop_client.api_key_store import save_api_key_hash
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.mngr.primitives import AgentId


def _build_authenticated_client(tmp_path: Path) -> tuple[TestClient, str]:
    """Build a TestClient + a valid Bearer API key for the v1 router."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, AgentId(), hash_api_key(api_key))
    # Reuse the freshly-minted AgentId by re-issuing the hash for a
    # second canonical agent; tests don't care about the caller id, only
    # that one valid key exists.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
    )
    return TestClient(app, base_url="http://localhost"), api_key


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# -- Auth --


def test_get_rejects_missing_auth(tmp_path: Path) -> None:
    client, _api_key = _build_authenticated_client(tmp_path)
    response = client.get("/api/v1/file-server", params={"path": str(tmp_path)})
    assert response.status_code == 401


def test_post_rejects_missing_auth(tmp_path: Path) -> None:
    client, _api_key = _build_authenticated_client(tmp_path)
    response = client.post("/api/v1/file-server", params={"path": str(tmp_path / "new")}, content=b"hi")
    assert response.status_code == 401


def test_get_rejects_invalid_bearer_token(tmp_path: Path) -> None:
    client, _api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(tmp_path)},
        headers={"Authorization": "Bearer not-a-real-key"},
    )
    assert response.status_code == 401


# -- Path validation --


def test_get_requires_absolute_path(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": "relative/path", "operation": "READ"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 400
    assert "absolute" in response.json()["detail"].lower()


def test_get_requires_path_param(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"operation": "READ"},
        headers=_auth_headers(api_key),
    )
    # FastAPI raises 422 when a required query param is missing.
    assert response.status_code == 422


# -- GET READ --


def test_get_read_returns_file_bytes(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "hello.txt"
    target.write_bytes(b"hello world")
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(target), "operation": "READ"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    assert response.content == b"hello world"


def test_get_read_uses_read_as_default_operation(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "default-op.bin"
    target.write_bytes(b"\x00\x01\x02")
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(target)},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    assert response.content == b"\x00\x01\x02"


def test_get_read_returns_404_for_missing_path(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(tmp_path / "does-not-exist"), "operation": "READ"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 404


def test_get_read_refuses_directory(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(tmp_path), "operation": "READ"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 400
    assert "not a regular file" in response.json()["detail"]


# -- GET LIST --


def test_get_list_returns_sorted_entries_with_metadata(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    # ``_build_authenticated_client`` plants a ``minds`` subdirectory in
    # ``tmp_path`` for state; carve out a clean sub-directory here so the
    # listing has a predictable set of entries.
    listing_dir = tmp_path / "listing-root"
    listing_dir.mkdir()
    (listing_dir / "b.txt").write_bytes(b"bb")
    (listing_dir / "a.txt").write_bytes(b"a")
    (listing_dir / "sub").mkdir()

    response = client.get(
        "/api/v1/file-server",
        params={"path": str(listing_dir), "operation": "LIST"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(listing_dir)
    entry_names = [entry["name"] for entry in body["entries"]]
    assert entry_names == ["a.txt", "b.txt", "sub"]
    by_name = {entry["name"]: entry for entry in body["entries"]}
    assert by_name["a.txt"]["type"] == "FILE"
    assert by_name["a.txt"]["size_bytes"] == 1
    assert by_name["sub"]["type"] == "DIRECTORY"
    # modified_at should be a parseable ISO-8601 string.
    assert "T" in by_name["a.txt"]["modified_at"]


def test_get_list_returns_400_for_file(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "a-file"
    target.write_bytes(b"x")
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(target), "operation": "LIST"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 400
    assert "not a directory" in response.json()["detail"]


def test_get_list_returns_404_for_missing(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(tmp_path / "nope"), "operation": "LIST"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 404


# -- GET STAT --


def test_get_stat_for_file(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "data.bin"
    target.write_bytes(b"abcdef")
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(target), "operation": "STAT"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == str(target)
    assert body["type"] == "FILE"
    assert body["size_bytes"] == 6


def test_get_stat_for_directory(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(tmp_path), "operation": "STAT"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "DIRECTORY"


def test_get_stat_for_symlink_reports_symlink(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "real.txt"
    target.write_bytes(b"hi")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(link), "operation": "STAT"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    assert response.json()["type"] == "SYMLINK"


def test_get_stat_returns_404_for_missing(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(tmp_path / "nope"), "operation": "STAT"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 404


def test_get_rejects_unknown_operation(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/file-server",
        params={"path": str(tmp_path), "operation": "DELETE"},
        headers=_auth_headers(api_key),
    )
    # FastAPI's enum coercion rejects unknown values with 422.
    assert response.status_code == 422


# -- POST write --


def test_post_writes_new_file(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "fresh.txt"
    response = client.post(
        "/api/v1/file-server",
        params={"path": str(target)},
        headers=_auth_headers(api_key),
        content=b"hello",
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"path": str(target), "bytes_written": 5}
    assert target.read_bytes() == b"hello"


def test_post_creates_missing_parent_directories(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "nested" / "deep" / "file.txt"
    response = client.post(
        "/api/v1/file-server",
        params={"path": str(target)},
        headers=_auth_headers(api_key),
        content=b"deep",
    )
    assert response.status_code == 200
    assert target.read_bytes() == b"deep"


def test_post_refuses_existing_file_by_default(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "existing.txt"
    target.write_bytes(b"original")
    response = client.post(
        "/api/v1/file-server",
        params={"path": str(target)},
        headers=_auth_headers(api_key),
        content=b"new",
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    # File on disk must be untouched.
    assert target.read_bytes() == b"original"


def test_post_overwrite_replaces_existing_file(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "existing.txt"
    target.write_bytes(b"original")
    response = client.post(
        "/api/v1/file-server",
        params={"path": str(target), "overwrite": "true"},
        headers=_auth_headers(api_key),
        content=b"new contents",
    )
    assert response.status_code == 200
    assert target.read_bytes() == b"new contents"


def test_post_refuses_to_write_to_directory(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    # ``overwrite=true`` so we get past the exists check and reach the
    # is-a-directory check we actually want to exercise.
    response = client.post(
        "/api/v1/file-server",
        params={"path": str(tmp_path), "overwrite": "true"},
        headers=_auth_headers(api_key),
        content=b"nope",
    )
    assert response.status_code == 400
    assert "directory" in response.json()["detail"].lower()


def test_post_requires_absolute_path(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    response = client.post(
        "/api/v1/file-server",
        params={"path": "relative/file.txt"},
        headers=_auth_headers(api_key),
        content=b"x",
    )
    assert response.status_code == 400


def test_post_writes_empty_body(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "empty.txt"
    response = client.post(
        "/api/v1/file-server",
        params={"path": str(target)},
        headers=_auth_headers(api_key),
        content=b"",
    )
    assert response.status_code == 200
    assert json.loads(response.text)["bytes_written"] == 0
    assert target.read_bytes() == b""


def test_post_writes_binary_body_verbatim(tmp_path: Path) -> None:
    client, api_key = _build_authenticated_client(tmp_path)
    target = tmp_path / "blob.bin"
    payload = bytes(range(256))
    response = client.post(
        "/api/v1/file-server",
        params={"path": str(target)},
        headers=_auth_headers(api_key),
        content=payload,
    )
    assert response.status_code == 200
    assert target.read_bytes() == payload
