from pathlib import Path

from imbue.minds.desktop_client.app import _with_home_assets
from imbue.minds.desktop_client.conftest import GOOD_APP_ICON
from imbue.minds.desktop_client.conftest import make_apps_seeded_resolver
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.wallpaper_cache import FetchedWallpaper
from imbue.minds.desktop_client.wallpaper_cache import WallpaperCache
from imbue.minds.desktop_client.wallpaper_cache import content_version
from imbue.minds.desktop_client.workspace_apps import icon_version_of

_AGENT_ID = "agent-" + "f" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 32


def _entry(create_attempt_state: str = "", is_remote: bool = False) -> UiWorkspaceEntry:
    return UiWorkspaceEntry(
        id=_AGENT_ID, name="alpha", accent="#aabbcc", create_attempt_state=create_attempt_state, is_remote=is_remote
    )


def test_a_live_owned_row_carries_its_apps_and_its_cached_wallpapers_version(tmp_path: Path) -> None:
    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers")
    cache.store(_AGENT_ID, FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG))

    entry = _with_home_assets(_entry(), make_apps_seeded_resolver(_AGENT_ID), cache)

    assert [(app.name, app.display_name, app.icon_version) for app in entry.apps] == [
        ("reading-list", "Reading List", icon_version_of(GOOD_APP_ICON)),
        ("zenbox", "zenbox", ""),
    ]
    assert entry.wallpaper_version == content_version(_PNG)


def test_a_row_with_no_cache_or_no_registry_keeps_empty_assets(tmp_path: Path) -> None:
    bare = _with_home_assets(_entry(), make_apps_seeded_resolver("agent-" + "0" * 32), None)
    assert bare.apps == ()
    assert bare.wallpaper_version == ""

    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers")
    unseen = _with_home_assets(_entry(), make_apps_seeded_resolver(_AGENT_ID), cache)
    assert unseen.wallpaper_version == ""


def test_create_attempts_and_remote_records_are_left_without_assets(tmp_path: Path) -> None:
    resolver = make_apps_seeded_resolver(_AGENT_ID)
    cache = WallpaperCache(cache_dir=tmp_path / "wallpapers")
    cache.store(_AGENT_ID, FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG))

    creating = _with_home_assets(_entry(create_attempt_state="creating"), resolver, cache)
    remote = _with_home_assets(_entry(is_remote=True), resolver, cache)

    assert (creating.apps, creating.wallpaper_version) == ((), "")
    assert (remote.apps, remote.wallpaper_version) == ((), "")
