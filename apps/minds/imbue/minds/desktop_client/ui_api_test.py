import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.ui_api import _sanitize_accent
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR


def _write_manifest(tmp_path: Path) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "src/boot.ts": {
                    "file": "assets/boot-AbC123.js",
                    "isEntry": True,
                    "css": ["assets/boot-DeF456.css"],
                },
                "src/other-chunk.ts": {"file": "assets/chunk-XyZ789.js"},
            }
        )
    )
    return manifest_path


def test_ui_index_redirects_to_login_when_unauthenticated(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.get("/ui/")

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


def test_ui_index_serves_not_built_page_when_manifest_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(tmp_path / "does-not-exist.json"))
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/")

    assert response.status_code == 503
    assert b"Frontend not built" in response.data


def test_ui_index_inlines_bootstrap_and_hashed_asset_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_manifest(tmp_path)))
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<script type="module" src="/_static/ui/assets/boot-AbC123.js"></script>' in html
    assert '<link rel="stylesheet" href="/_static/ui/assets/boot-DeF456.css">' in html
    # Non-entry chunks are not emitted as tags.
    assert "chunk-XyZ789" not in html
    # The bootstrap document is inlined with the full snapshot.
    assert "window.__MINDS_BOOTSTRAP__ = " in html
    bootstrap_json = html.split("window.__MINDS_BOOTSTRAP__ = ", 1)[1].split(";</script>", 1)[0]
    bootstrap = json.loads(bootstrap_json)
    assert bootstrap["schema_version"] == 1
    assert bootstrap["snapshot"]["workspaces"]["type"] == "workspaces"
    assert bootstrap["seed"]["mngr_forward_origin"].startswith("https://localhost:")


def test_ui_index_bootstrap_seed_reflects_mac_user_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_manifest(tmp_path)))
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/", headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})

    bootstrap_json = (
        response.get_data(as_text=True).split("window.__MINDS_BOOTSTRAP__ = ", 1)[1].split(";</script>", 1)[0]
    )
    assert json.loads(bootstrap_json)["seed"]["is_mac"] is True


def test_ui_blueprint_registers_index_ws_and_area_stubs(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)
    rules = {rule.rule for rule in client.application.url_map.iter_rules()}
    assert "/ui/" in rules
    assert "/ui/ws" in rules
    # One representative rule per per-area register_*_routes group, so a
    # module dropped from create_ui_blueprint's fan-out fails here.
    assert "/ui/api/create/form-defaults" in rules
    assert "/ui/api/settings" in rules
    assert "/ui/api/inbox" in rules
    assert "/ui/api/onboarding/consent" in rules
    assert any(rule.startswith("/ui/api/workspaces/") for rule in rules)
    assert any(rule.startswith("/ui/api/destroyed-workspaces/") for rule in rules)


def test_app_status_unauthenticated_discloses_nothing(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.get("/ui/api/app-status")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload == {"is_authenticated": False, "restorable_workspace_ids": []}


def test_app_status_authenticated_carries_startup_router_inputs(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/app-status")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["is_authenticated"] is True
    assert isinstance(payload["restorable_workspace_ids"], list)
    assert isinstance(payload["has_accounts"], bool)
    assert isinstance(payload["workspace_count"], int)
    assert isinstance(payload["needs_error_reporting_consent"], bool)


def test_sanitize_accent_accepts_hex_and_rejects_anything_else() -> None:
    """The accent query param is inlined into the page's CSS custom property,
    so only plain hex colors may pass through."""
    assert _sanitize_accent("#aabbcc") == "#aabbcc"
    assert _sanitize_accent("#AB1") == "#AB1"
    assert _sanitize_accent(None) == DEFAULT_WORKSPACE_COLOR
    assert _sanitize_accent("") == DEFAULT_WORKSPACE_COLOR
    assert _sanitize_accent("url(https://evil.example/x)") == DEFAULT_WORKSPACE_COLOR
    assert _sanitize_accent("#aabbcc; background: red") == DEFAULT_WORKSPACE_COLOR
