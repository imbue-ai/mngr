import json
import os
from pathlib import Path

import pytest

from imbue.mngr_forward.service_map_cache import PersistedServiceMap
from imbue.mngr_forward.service_map_cache import ServiceMapCache


def _empty() -> PersistedServiceMap:
    return PersistedServiceMap(services_by_instance={}, label_to_name_by_instance={})


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    assert cache.load() == _empty()


def test_persist_then_load_roundtrip_with_labels(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    payload = PersistedServiceMap(
        services_by_instance={
            "agent-a@host-1": {"system_interface": "http://127.0.0.1:8000", "web": "http://127.0.0.1:8080"},
            "agent-b@host-1": {"system_interface": "http://127.0.0.1:8000"},
        },
        label_to_name_by_instance={
            "agent-a@host-1": {"web-x7k9q2w1": "web", "system_interface-a1b2c3d4": "system_interface"},
        },
    )
    cache.persist(payload)
    assert cache.load() == payload


def test_persist_overwrites_previous_contents(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    cache.persist(
        PersistedServiceMap(
            services_by_instance={"agent-a@host-1": {"system_interface": "http://127.0.0.1:8000"}},
            label_to_name_by_instance={},
        )
    )
    replacement = PersistedServiceMap(
        services_by_instance={"agent-b@host-1": {"system_interface": "http://127.0.0.1:9000"}},
        label_to_name_by_instance={"agent-b@host-1": {"system_interface-zz11": "system_interface"}},
    )
    cache.persist(replacement)
    assert cache.load() == replacement


def test_load_legacy_unversioned_format_yields_services_with_empty_labels(tmp_path: Path) -> None:
    # A cache written before the format was versioned is the bare services map
    # as the whole document. It must load as services-only so an upgraded
    # forward keeps its warm-start, with labels arriving from the live stream.
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text('{"agent-a@host-1": {"system_interface": "http://127.0.0.1:8000"}}')
    assert ServiceMapCache(cache_path=cache_path).load() == PersistedServiceMap(
        services_by_instance={"agent-a@host-1": {"system_interface": "http://127.0.0.1:8000"}},
        label_to_name_by_instance={},
    )


def test_load_unknown_future_format_version_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text('{"format_version": 3, "something_new": {"agent-a@host-1": {"x": "y"}}}')
    assert ServiceMapCache(cache_path=cache_path).load() == _empty()


def test_versioned_file_degrades_to_empty_under_the_legacy_reader(tmp_path: Path) -> None:
    # Rollout safety: an older `mngr forward` reads the whole document as the
    # services map and coerces each entry to {str: str}. On a version-2 file
    # every top-level value is either an int or a dict-of-dicts, so the legacy
    # coercion must yield nothing -- an empty seed, never a bad route. This
    # mirrors the old _coerce_service_map logic verbatim.
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    cache.persist(
        PersistedServiceMap(
            services_by_instance={"agent-a@host-1": {"system_interface": "http://127.0.0.1:8000"}},
            label_to_name_by_instance={"agent-a@host-1": {"web-x7k9q2w1": "web"}},
        )
    )
    raw = json.loads((tmp_path / "service_map.json").read_text())
    legacy_view: dict[str, dict[str, str]] = {}
    for agent_id, services in raw.items():
        if not isinstance(services, dict):
            continue
        clean = {name: url for name, url in services.items() if isinstance(name, str) and isinstance(url, str)}
        if clean:
            legacy_view[agent_id] = clean
    assert legacy_view == {}


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text("{not valid json")
    assert ServiceMapCache(cache_path=cache_path).load() == _empty()


def test_load_invalid_utf8_returns_empty(tmp_path: Path) -> None:
    # A cache file with non-UTF-8 bytes is malformed; load must degrade to
    # empty rather than leaking UnicodeDecodeError, since load runs on the
    # forward startup critical path via resolver.seed_services.
    cache_path = tmp_path / "service_map.json"
    cache_path.write_bytes(b"\xff\xfe garbage")
    assert ServiceMapCache(cache_path=cache_path).load() == _empty()


def test_load_non_object_json_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text('["a", "b"]')
    assert ServiceMapCache(cache_path=cache_path).load() == _empty()


def test_load_drops_malformed_entries_in_both_maps(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    # A mix of a valid entry, a non-dict value, a dict with a non-string URL,
    # and a dict that becomes empty after cleaning. Only the valid entries
    # survive, independently per map.
    cache_path.write_text(
        json.dumps(
            {
                "format_version": 2,
                "services_by_instance": {
                    "agent-good@host-1": {"system_interface": "http://127.0.0.1:8000"},
                    "agent-bad-value@host-1": "not-a-dict",
                    "agent-bad-url@host-1": {"system_interface": 8000},
                    "agent-empty@host-1": {"system_interface": None},
                },
                "label_to_name_by_instance": {
                    "agent-good@host-1": {"web-x7k9q2w1": "web"},
                    "agent-bad-label@host-1": {"web-x7k9q2w1": 5},
                },
            }
        )
    )
    assert ServiceMapCache(cache_path=cache_path).load() == PersistedServiceMap(
        services_by_instance={"agent-good@host-1": {"system_interface": "http://127.0.0.1:8000"}},
        label_to_name_by_instance={"agent-good@host-1": {"web-x7k9q2w1": "web"}},
    )


def test_load_tolerates_missing_map_keys_in_versioned_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text('{"format_version": 2}')
    assert ServiceMapCache(cache_path=cache_path).load() == _empty()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permission checks, so the file stays readable",
)
def test_load_swallows_read_error(tmp_path: Path) -> None:
    # An existing-but-unreadable cache file must degrade to empty rather than
    # raising, so a permission/IO error can never break forward startup
    # (load runs on the startup critical path via resolver.seed_services).
    cache_path = tmp_path / "service_map.json"
    cache_path.write_text('{"agent-a@host-1": {"system_interface": "http://127.0.0.1:8000"}}')
    cache_path.chmod(0o000)
    try:
        assert ServiceMapCache(cache_path=cache_path).load() == _empty()
    finally:
        cache_path.chmod(0o600)


def test_persist_swallows_write_error(tmp_path: Path) -> None:
    # Point the cache at a path whose parent is a regular file, so the atomic
    # write's mkdir fails. persist must log and swallow rather than raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    cache = ServiceMapCache(cache_path=blocker / "service_map.json")
    cache.persist(
        PersistedServiceMap(
            services_by_instance={"agent-a@host-1": {"system_interface": "http://127.0.0.1:8000"}},
            label_to_name_by_instance={},
        )
    )
    assert cache.load() == _empty()
