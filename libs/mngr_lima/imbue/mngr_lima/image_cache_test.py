import hashlib
import sys
from pathlib import Path

import pytest

from imbue.mngr_lima.image_cache import compute_file_sha256
from imbue.mngr_lima.image_cache import get_lima_cache_data_path
from imbue.mngr_lima.image_cache import is_image_cached
from imbue.mngr_lima.image_cache import wait_for_image_ready


def test_get_lima_cache_data_path_is_url_addressable() -> None:
    path = get_lima_cache_data_path("https://example.com/image.qcow2")
    expected_hash = hashlib.sha256(b"https://example.com/image.qcow2").hexdigest()
    assert path.name == "data"
    assert path.parent.name == expected_hash
    assert path.parent.parent.name == "by-url-sha256"


def test_get_lima_cache_data_path_macos_root() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS-specific path layout")
    path = get_lima_cache_data_path("https://example.com/image.qcow2")
    assert "Library/Caches/lima" in str(path)


def test_get_lima_cache_data_path_distinct_for_distinct_urls() -> None:
    a = get_lima_cache_data_path("https://example.com/a.qcow2")
    b = get_lima_cache_data_path("https://example.com/b.qcow2")
    assert a != b


def test_compute_file_sha256(tmp_path: Path) -> None:
    payload = b"hello world"
    target = tmp_path / "blob"
    target.write_bytes(payload)
    assert compute_file_sha256(target) == hashlib.sha256(payload).hexdigest()


def test_is_image_cached_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect HOME so the cache lookup resolves inside tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert is_image_cached("https://example.com/a.qcow2", expected_sha256=None) is False


def test_is_image_cached_present_no_digest_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    url = "https://example.com/a.qcow2"
    cache_path = get_lima_cache_data_path(url)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"dummy")
    assert is_image_cached(url, expected_sha256=None) is True


def test_is_image_cached_digest_mismatch_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    url = "https://example.com/a.qcow2"
    cache_path = get_lima_cache_data_path(url)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"dummy")
    assert is_image_cached(url, expected_sha256="deadbeef") is False


def test_is_image_cached_digest_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    url = "https://example.com/a.qcow2"
    payload = b"real image bytes"
    cache_path = get_lima_cache_data_path(url)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert is_image_cached(url, expected_sha256=digest) is True


def test_wait_for_image_ready_returns_false_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    url = "https://example.com/never.qcow2"
    result = wait_for_image_ready(url, expected_sha256=None, timeout_seconds=0.1, poll_interval_seconds=0.05)
    assert result is False


def test_wait_for_image_ready_returns_true_when_file_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    url = "https://example.com/a.qcow2"
    cache_path = get_lima_cache_data_path(url)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"dummy")
    result = wait_for_image_ready(url, expected_sha256=None, timeout_seconds=1.0, poll_interval_seconds=0.05)
    assert result is True
