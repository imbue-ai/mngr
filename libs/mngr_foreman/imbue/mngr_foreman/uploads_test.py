"""Tests for chat-upload name sanitising and host write/delete."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from imbue.mngr_foreman import uploads
from imbue.mngr_foreman.uploads import MAX_UPLOAD_BYTES
from imbue.mngr_foreman.uploads import UploadError
from imbue.mngr_foreman.uploads import delete_upload
from imbue.mngr_foreman.uploads import sanitize_stored_name
from imbue.mngr_foreman.uploads import write_upload


class _FakeHost:
    def __init__(self, rm_success: bool = True) -> None:
        self.written: dict[str, bytes] = {}
        self.commands: list[str] = []
        self._rm_success = rm_success

    def write_file(self, path: Path, content: bytes) -> None:
        self.written[str(path)] = content

    def execute_stateful_command(self, command: str) -> object:
        self.commands.append(command)
        return SimpleNamespace(success=self._rm_success, stdout="", stderr="permission denied")


def _patch_resolve(monkeypatch: pytest.MonkeyPatch, host: _FakeHost, work_dir: str = "/home/agent/work") -> None:
    agent = SimpleNamespace(work_dir=Path(work_dir))
    monkeypatch.setattr(uploads, "_resolve_started_agent_and_host", lambda ctx, name: (agent, host))


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
    # Only the final extension matters; a dotted stem is fine.
    assert sanitize_stored_name("archive.tar.gz") == "archive.tar.gz"


# ---- write_upload ----


def test_write_upload_writes_under_chat_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _patch_resolve(monkeypatch, host)
    rel = write_upload(SimpleNamespace(), "worker", "img.png", b"\x89PNG\x00\xff")
    assert rel == "./chat_uploads/img.png"
    assert host.written == {"/home/agent/work/chat_uploads/img.png": b"\x89PNG\x00\xff"}


def test_write_upload_rejects_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _patch_resolve(monkeypatch, host)
    with pytest.raises(UploadError):
        write_upload(SimpleNamespace(), "worker", "big.bin", b"x" * (MAX_UPLOAD_BYTES + 1))
    assert host.written == {}  # never touched the host


def test_write_upload_rejects_bad_name(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _patch_resolve(monkeypatch, host)
    with pytest.raises(UploadError):
        write_upload(SimpleNamespace(), "worker", "../escape.png", b"data")
    assert host.written == {}


def test_write_upload_wraps_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(ctx: object, name: str) -> object:
        raise RuntimeError("host offline")

    monkeypatch.setattr(uploads, "_resolve_started_agent_and_host", _boom)
    with pytest.raises(UploadError, match="could not resolve agent host"):
        write_upload(SimpleNamespace(), "worker", "img.png", b"data")


# ---- delete_upload ----


def test_delete_upload_runs_rm(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _patch_resolve(monkeypatch, host)
    delete_upload(SimpleNamespace(), "worker", "img.png")
    assert len(host.commands) == 1
    assert "rm -f" in host.commands[0]
    assert "/home/agent/work/chat_uploads/img.png" in host.commands[0]


def test_delete_upload_raises_on_rm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost(rm_success=False)
    _patch_resolve(monkeypatch, host)
    with pytest.raises(UploadError):
        delete_upload(SimpleNamespace(), "worker", "img.png")
