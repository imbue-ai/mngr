"""Tests for chat-upload name sanitising and host write/read/delete via the pool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import Callable
from typing import cast

import pytest

from imbue.mngr_foreman import uploads
from imbue.mngr_foreman.connection_pool import ConnectionPool
from imbue.mngr_foreman.uploads import MAX_UPLOAD_BYTES
from imbue.mngr_foreman.uploads import UploadError
from imbue.mngr_foreman.uploads import UploadNotFound
from imbue.mngr_foreman.uploads import content_type_for_name
from imbue.mngr_foreman.uploads import delete_upload
from imbue.mngr_foreman.uploads import read_upload
from imbue.mngr_foreman.uploads import sanitize_stored_name
from imbue.mngr_foreman.uploads import write_upload


@pytest.fixture(autouse=True)
def _clear_serve_cache() -> None:
    uploads._SERVE_CACHE.clear()


class _FakeHost:
    def __init__(self, rm_success: bool = True, read_bytes: bytes | None = None) -> None:
        self.written: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.read_calls = 0
        self._rm_success = rm_success
        self._read_bytes = read_bytes

    def write_file(self, path: Path, content: bytes) -> None:
        self.written[str(path)] = content

    def read_file(self, path: Path) -> bytes:
        self.read_calls += 1
        if self._read_bytes is None:
            raise FileNotFoundError(str(path))
        return self._read_bytes

    def execute_stateful_command(self, command: str) -> object:
        self.commands.append(command)
        return SimpleNamespace(success=self._rm_success, stdout="", stderr="permission denied")


class _FakePool:
    """Stands in for ConnectionPool: runs fn against a fake agent + host, or fails."""

    def __init__(self, host: _FakeHost, work_dir: str = "/home/agent/work", fail: Exception | None = None) -> None:
        self._host = host
        self._agent = SimpleNamespace(work_dir=Path(work_dir))
        self._fail = fail

    def run_on_host(self, agent_name: str, fn: Callable[[Any, Any], Any]) -> Any:
        if self._fail is not None:
            raise self._fail
        return fn(self._agent, self._host)


def _pool(host: _FakeHost, **kwargs: Any) -> ConnectionPool:
    return cast(ConnectionPool, _FakePool(host, **kwargs))


# ---- sanitize_stored_name ----


def test_sanitize_accepts_uuid_ext() -> None:
    assert sanitize_stored_name("a1b2c3d4-e5f6-4a7b-8c9d-0123456789ab.png") == "a1b2c3d4-e5f6-4a7b-8c9d-0123456789ab.png"
    assert sanitize_stored_name("photo123.jpeg") == "photo123.jpeg"


def test_sanitize_rejects_path_traversal() -> None:
    for bad in ("../etc/passwd", "..", "a/../b.png", "foo/bar.png", "/abs/path.png"):
        with pytest.raises(UploadError):
            sanitize_stored_name(bad)


def test_sanitize_rejects_missing_extension() -> None:
    for bad in ("noext", "trailingdot."):
        with pytest.raises(UploadError):
            sanitize_stored_name(bad)


def test_sanitize_rejects_leading_dot_and_bad_chars() -> None:
    for bad in (".hidden.png", "bad$.png", "space name.png", "emoji😀.png"):
        with pytest.raises(UploadError):
            sanitize_stored_name(bad)


def test_sanitize_rejects_overlong_extension() -> None:
    with pytest.raises(UploadError):
        sanitize_stored_name("x." + "a" * 13)


def test_sanitize_allows_multi_dot_stem() -> None:
    assert sanitize_stored_name("archive.tar.gz") == "archive.tar.gz"


# ---- write_upload ----


def test_write_upload_writes_under_chat_uploads() -> None:
    host = _FakeHost()
    rel = write_upload(_pool(host), "worker", "img.png", b"\x89PNG\x00\xff")
    assert rel == "./chat_uploads/img.png"
    assert host.written == {"/home/agent/work/chat_uploads/img.png": b"\x89PNG\x00\xff"}


def test_write_upload_rejects_oversize() -> None:
    host = _FakeHost()
    with pytest.raises(UploadError):
        write_upload(_pool(host), "worker", "big.bin", b"x" * (MAX_UPLOAD_BYTES + 1))
    assert host.written == {}  # never touched the host


def test_write_upload_rejects_bad_name() -> None:
    host = _FakeHost()
    with pytest.raises(UploadError):
        write_upload(_pool(host), "worker", "../escape.png", b"data")
    assert host.written == {}


def test_write_upload_wraps_host_failure() -> None:
    host = _FakeHost()
    pool = _pool(host, fail=RuntimeError("host offline"))
    with pytest.raises(UploadError, match="could not write to agent host"):
        write_upload(pool, "worker", "img.png", b"data")


# ---- delete_upload ----


def test_delete_upload_runs_rm() -> None:
    host = _FakeHost()
    delete_upload(_pool(host), "worker", "img.png")
    assert len(host.commands) == 1
    assert "rm -f" in host.commands[0]
    assert "/home/agent/work/chat_uploads/img.png" in host.commands[0]


def test_delete_upload_raises_on_rm_failure() -> None:
    host = _FakeHost(rm_success=False)
    with pytest.raises(UploadError):
        delete_upload(_pool(host), "worker", "img.png")


# ---- read_upload / serving ----


def test_read_upload_returns_bytes() -> None:
    host = _FakeHost(read_bytes=b"\x89PNG-data")
    assert read_upload(_pool(host), "worker", "img.png") == b"\x89PNG-data"


def test_read_upload_caches_second_call() -> None:
    host = _FakeHost(read_bytes=b"cached")
    pool = _pool(host)
    read_upload(pool, "worker", "img.png")
    read_upload(pool, "worker", "img.png")
    assert host.read_calls == 1  # the second read is served from cache


def test_read_upload_missing_raises_not_found() -> None:
    host = _FakeHost(read_bytes=None)  # read_file raises FileNotFoundError
    with pytest.raises(UploadNotFound):
        read_upload(_pool(host), "worker", "gone.png")


def test_read_upload_rejects_bad_name() -> None:
    host = _FakeHost(read_bytes=b"x")
    with pytest.raises(UploadError):
        read_upload(_pool(host), "worker", "../secret")


def test_delete_upload_invalidates_cache() -> None:
    host = _FakeHost(read_bytes=b"v1")
    pool = _pool(host)
    read_upload(pool, "worker", "img.png")  # populate cache
    delete_upload(pool, "worker", "img.png")  # should evict
    assert ("worker", "img.png") not in uploads._SERVE_CACHE


def test_content_type_for_name() -> None:
    assert content_type_for_name("a.png") == "image/png"
    assert content_type_for_name("a.JPG") == "image/jpeg"
    assert content_type_for_name("a.svg") == "image/svg+xml"
    assert content_type_for_name("a.bin") == "application/octet-stream"
    assert content_type_for_name("noext") == "application/octet-stream"
