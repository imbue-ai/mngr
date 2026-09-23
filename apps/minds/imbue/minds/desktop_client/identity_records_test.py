import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.desktop_client.identity_records import IdentityCache
from imbue.minds.desktop_client.identity_records import IdentityRecord
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError

_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _record(user_id: str, email: str | None = "a@example.com") -> IdentityRecord:
    return IdentityRecord(user_id=user_id, email=email, display_name="Al", profile_picture_url=None)


def test_identity_cache_round_trips_records_through_its_file(tmp_path: Path) -> None:
    cache = IdentityCache(path=tmp_path / "identity_cache.json")
    assert cache.get("user-1") is None

    cache.put(_record("user-1"), _NOW)

    reloaded = IdentityCache(path=tmp_path / "identity_cache.json").get("user-1")
    assert reloaded is not None
    assert reloaded.record == _record("user-1")
    assert reloaded.fetched_at == _NOW


def test_identity_cache_serves_fresh_entries_without_fetching(tmp_path: Path) -> None:
    cache = IdentityCache(path=tmp_path / "identity_cache.json")
    cache.put(_record("user-1"), _NOW)

    def _fetch(user_id: str) -> IdentityRecord | None:
        raise AssertionError(f"unexpected fetch of {user_id}")

    assert cache.get_or_fetch("user-1", _fetch, _NOW + timedelta(hours=1)) == _record("user-1")


def test_identity_cache_refreshes_stale_entries_and_falls_back_to_them_on_failure(tmp_path: Path) -> None:
    cache = IdentityCache(path=tmp_path / "identity_cache.json")
    cache.put(_record("user-1", email="old@example.com"), _NOW)
    later = _NOW + timedelta(days=2)

    refreshed = cache.get_or_fetch("user-1", lambda _user_id: _record("user-1", email="new@example.com"), later)
    assert refreshed is not None
    assert refreshed.email == "new@example.com"
    cached = cache.get("user-1")
    assert cached is not None and cached.fetched_at == later

    def _failing(_user_id: str) -> IdentityRecord | None:
        raise ImbueCloudCliError("connector down")

    stale = cache.get_or_fetch("user-1", _failing, later + timedelta(days=2))
    assert stale is not None and stale.email == "new@example.com"
    # With nothing cached, the failure propagates.
    with pytest.raises(ImbueCloudCliError):
        cache.get_or_fetch("user-2", _failing, later)
    # An unknown user fetches to None and caches nothing.
    assert cache.get_or_fetch("user-3", lambda _user_id: None, later) is None
    assert cache.get("user-3") is None


def test_identity_cache_treats_an_unreadable_file_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "identity_cache.json"
    path.write_text("{not json")
    cache = IdentityCache(path=path)
    assert cache.get("user-1") is None
    cache.put(_record("user-1"), _NOW)
    assert json.loads(path.read_text())["entries_by_user_id"]["user-1"]["record"]["user_id"] == "user-1"
