import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.conftest import GOOD_APP_ICON
from imbue.minds.desktop_client.conftest import SHARED_TEST_HOST_ID
from imbue.minds.desktop_client.conftest import SHARED_TEST_WORKSPACE_ID
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_apps_seeded_resolver
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_shared_share
from imbue.minds.desktop_client.conftest import make_shared_with_me_store_for_test
from imbue.minds.desktop_client.conftest import seed_shared_with_me_listing
from imbue.minds.desktop_client.shared_with_me_gate import NOT_OWNED_ERROR_CODE
from imbue.minds.desktop_client.wallpaper_cache import CACHE_DIRNAME
from imbue.minds.desktop_client.wallpaper_cache import FetchedWallpaper
from imbue.minds.desktop_client.wallpaper_cache import WallpaperCache
from imbue.minds.desktop_client.wallpaper_cache import content_version

_AGENT_ID = "agent-" + "d" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
_RECIPIENT_USER_ID = "33333333-3333-3333-3333-333333333333"
_RECIPIENT_EMAIL = "viewer@example.com"


def _client(tmp_path: Path, is_authenticated: bool = True, **create_kwargs: object) -> FlaskClient:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=make_apps_seeded_resolver(_AGENT_ID),
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        **create_kwargs,
    )
    return client


def test_app_icon_serves_the_accepted_markup_immutably_and_without_scripts(tmp_path: Path) -> None:
    response = _client(tmp_path).get(f"/ui/api/workspaces/{_AGENT_ID}/apps/reading-list/icon.svg?v=abc")

    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert response.get_data(as_text=True) == GOOD_APP_ICON
    assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'"


def test_app_icon_is_absent_for_an_unusable_icon_a_system_app_an_unknown_app_and_a_bad_id(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get(f"/ui/api/workspaces/{_AGENT_ID}/apps/zenbox/icon.svg").status_code == 404
    assert client.get(f"/ui/api/workspaces/{_AGENT_ID}/apps/terminal/icon.svg").status_code == 404
    assert client.get(f"/ui/api/workspaces/{_AGENT_ID}/apps/nope/icon.svg").status_code == 404
    assert client.get("/ui/api/workspaces/not-an-id/apps/reading-list/icon.svg").status_code == 404


def test_wallpaper_serves_the_cached_image_and_404s_without_one(tmp_path: Path) -> None:
    cache = WallpaperCache(cache_dir=tmp_path / "minds" / CACHE_DIRNAME)
    cache.store(_AGENT_ID, FetchedWallpaper(kind="file", name="dunes", content_type="image/png", body=_PNG))
    client = _client(tmp_path)

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/wallpaper?v={content_version(_PNG)}")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.get_data() == _PNG
    assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert client.get("/ui/api/workspaces/agent-" + "e" * 32 + "/wallpaper").status_code == 404
    assert client.get("/ui/api/workspaces/not-an-id/wallpaper").status_code == 404


def test_wallpaper_refresh_is_refused_on_an_app_with_no_forward_to_read_through(tmp_path: Path) -> None:
    response = _client(tmp_path).post(f"/ui/api/workspaces/{_AGENT_ID}/wallpaper/refresh")

    assert response.status_code == 503
    assert json.loads(response.get_data(as_text=True))["error"] == "Wallpapers cannot be read on this app"
    assert _client(tmp_path / "other").post("/ui/api/workspaces/not-an-id/wallpaper/refresh").status_code == 404


def test_wallpaper_refresh_schedules_a_fetch_and_answers_at_once(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # A forward to read through and a group to run on wire the refresher; the fetch it starts finds no CA
    # certificate under the test's mngr dir and ends without dialing anything.
    client = _client(
        tmp_path,
        mngr_forward_port=8421,
        mngr_forward_preauth_cookie="preauth-cookie",
        root_concurrency_group=root_concurrency_group,
        mngr_host_dir=tmp_path / "mngr",
    )

    response = client.post(f"/ui/api/workspaces/{_AGENT_ID}/wallpaper/refresh")

    assert response.status_code == 202
    assert json.loads(response.get_data(as_text=True)) == {"agent_id": _AGENT_ID, "is_started": True}
    # The id check comes before the schedule, refresher or not.
    assert client.post("/ui/api/workspaces/not-an-id/wallpaper/refresh").status_code == 404


def test_asset_routes_require_the_ui_session(tmp_path: Path) -> None:
    client = _client(tmp_path, is_authenticated=False)

    assert client.get(f"/ui/api/workspaces/{_AGENT_ID}/apps/reading-list/icon.svg").status_code == 401
    assert client.get(f"/ui/api/workspaces/{_AGENT_ID}/wallpaper").status_code == 401
    assert client.post(f"/ui/api/workspaces/{_AGENT_ID}/wallpaper/refresh").status_code == 401


def test_asset_routes_refuse_a_workspace_shared_with_an_account_here(tmp_path: Path) -> None:
    # The routes name the workspace by ``agent_id``, which the shared-with-me gate reads (specs/home-screen-redesign, 4.10).
    cli = make_fake_imbue_cloud_cli()
    seed_shared_with_me_listing(
        cli, _RECIPIENT_EMAIL, make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID)
    )
    store = make_shared_with_me_store_for_test(tmp_path / "minds", cli)
    assert store.refresh(_RECIPIENT_USER_ID, _RECIPIENT_EMAIL, datetime.now(timezone.utc)) is True
    client = _client(tmp_path, shared_with_me_store=store)

    icon = client.get(f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/apps/reading-list/icon.svg")
    wallpaper = client.get(f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/wallpaper")
    refresh = client.post(f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/wallpaper/refresh")

    for response in (icon, wallpaper, refresh):
        assert response.status_code == 403
        assert json.loads(response.get_data(as_text=True))["code"] == NOT_OWNED_ERROR_CODE
