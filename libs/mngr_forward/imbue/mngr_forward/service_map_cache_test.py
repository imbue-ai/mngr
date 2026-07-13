import os
from pathlib import Path

import pytest

from imbue.mngr_forward.service_map_cache import ServiceMapCache


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    assert cache.load() == {}


def test_persist_then_load_roundtrip(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    payload = {
        "agent-a": {"system_interface": "http://127.0.0.1:8000", "web": "http://127.0.0.1:8080"},
        "agent-b": {"system_interface": "http://127.0.0.1:8000"},
    }
    cache.persist(payload)
    assert cache.load() == payload


def test_persist_overwrites_previous_contents(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    cache.persist({"agent-a": {"system_interface": "http://127.0.0.1:8000"}})
    cache.persist({"agent-b": {"system_interface": "http://127.0.0.1:9000"}})
    assert cache.load() == {"agent-b": {"system_interface": "http://127.0.0.1:9000"}}


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text("{not valid json")
    assert ServiceMapCache(cache_path=cache_path).load() == {}


def test_load_invalid_utf8_returns_empty(tmp_path: Path) -> None:
    # A cache file with non-UTF-8 bytes is malformed; load must degrade to {}
    # rather than leaking UnicodeDecodeError, since load runs on the forward
    # startup critical path via resolver.seed_services.
    cache_path = tmp_path / "service_map.json"
    cache_path.write_bytes(b"\xff\xfe garbage")
    assert ServiceMapCache(cache_path=cache_path).load() == {}


def test_load_non_object_json_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text('["a", "b"]')
    assert ServiceMapCache(cache_path=cache_path).load() == {}


def test_load_drops_malformed_entries(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    # A mix of a valid entry, a non-dict value, a dict with a non-string URL,
    # and a dict that becomes empty after cleaning. Only the valid entry survives.
    cache_path.write_text(
        '{"agent-good": {"system_interface": "http://127.0.0.1:8000"}, '
        '"agent-bad-value": "not-a-dict", '
        '"agent-bad-url": {"system_interface": 8000}, '
        '"agent-empty": {"system_interface": null}}'
    )
    assert ServiceMapCache(cache_path=cache_path).load() == {
        "agent-good": {"system_interface": "http://127.0.0.1:8000"}
    }


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permission checks, so the file stays readable",
)
def test_load_swallows_read_error(tmp_path: Path) -> None:
    # An existing-but-unreadable cache file must degrade to {} rather than
    # raising, so a permission/IO error can never break forward startup
    # (load runs on the startup critical path via resolver.seed_services).
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text('{"agent-a": {"system_interface": "http://127.0.0.1:8000"}}')
    cache_path.chmod(0o000)
    try:
        assert ServiceMapCache(cache_path=cache_path).load() == {}
    finally:
        cache_path.chmod(0o600)


def test_persist_swallows_write_error(tmp_path: Path) -> None:
    # Point the cache at a path whose parent is a regular file, so the atomic
    # write's mkdir fails. persist must log and swallow rather than raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    cache = ServiceMapCache(cache_path=blocker / "service_map.json")
    cache.persist({"agent-a": {"system_interface": "http://127.0.0.1:8000"}})
    assert cache.load() == {}
