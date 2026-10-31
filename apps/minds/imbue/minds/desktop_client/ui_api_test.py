import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api import _record_recent_window_state
from imbue.minds.desktop_client.ui_api import _sanitize_accent
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiClientStateMessage
from imbue.minds.desktop_client.ui_models import UiWindowKind
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


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
    # The embed contract is an ES module the workspace frame imports on mount: preloaded, never
    # run as a classic script.
    assert '<link rel="modulepreload" href="/_static/embed_contract.js">' in html
    assert '<script src="/_static/embed_contract.js"></script>' not in html
    # The bootstrap document is inlined with the full snapshot.
    assert "window.__MINDS_BOOTSTRAP__ = " in html
    bootstrap_json = html.split("window.__MINDS_BOOTSTRAP__ = ", 1)[1].split(";</script>", 1)[0]
    bootstrap = json.loads(bootstrap_json)
    assert bootstrap["schema_version"] == UI_SCHEMA_VERSION
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


def test_ui_index_serves_an_accounts_home_page_with_and_without_its_trailing_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/account/<id>/`` is what a new or restored window loads; both spellings must serve the index on that account."""
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_manifest(tmp_path)))
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    for path in ("/account/local/", "/account/local"):
        response = client.get(path)

        assert response.status_code == 200, path
        html = response.get_data(as_text=True)
        bootstrap_json = html.split("window.__MINDS_BOOTSTRAP__ = ", 1)[1].split(";</script>", 1)[0]
        assert json.loads(bootstrap_json)["seed"]["account_id"] == "local"


def test_ui_index_bootstrap_seeds_a_workspace_urls_account_from_its_record_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored window names its workspace by host id; the owner comes from the records, discovery or not."""
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_manifest(tmp_path)))
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-alice", email="alice@example.com")
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)
    host_id = str(HostId.generate())
    # Discovery reports nothing: the workspace is a synced row this device never ran.
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        imbue_cloud_cli=cli,
        session_store=session_store,
    )
    session_store.associate_created_workspace(
        user_id="user-alice",
        agent_id=str(AgentId.generate()),
        host_id=host_id,
        display_name="ws",
        color=None,
        is_cloud_row=True,
    )

    def seed_account_id(path: str) -> str:
        html = client.get(path).get_data(as_text=True)
        bootstrap_json = html.split("window.__MINDS_BOOTSTRAP__ = ", 1)[1].split(";</script>", 1)[0]
        return json.loads(bootstrap_json)["seed"]["account_id"]

    assert seed_account_id(f"/workspace/{host_id}") == "user-alice"
    assert seed_account_id(f"/workspace/{HostId.generate()}") == "local"


def test_ui_index_omits_sentry_bootstrap_when_reporting_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_manifest(tmp_path)))
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    minds_config.set_report_unexpected_errors(False)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    html = client.get("/ui/").get_data(as_text=True)

    assert "minds-sentry-config" not in html
    assert "sentry.browser.min.js" not in html
    assert "sentry_init.js" not in html


def test_ui_index_inlines_sentry_bootstrap_when_reporting_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_manifest(tmp_path)))
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    minds_config.set_report_unexpected_errors(True)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    html = client.get("/ui/").get_data(as_text=True)

    # The config blob precedes the bundle + init tags, all before the SPA entry.
    assert '<script type="application/json" id="minds-sentry-config">' in html
    assert '<script src="/_static/sentry.browser.min.js"></script>' in html
    assert '<script src="/_static/sentry_init.js"></script>' in html
    sentry_json = html.split('id="minds-sentry-config">', 1)[1].split("</script>", 1)[0]
    sentry_config = json.loads(sentry_json)
    assert sentry_config["dsn"].startswith("https://")
    assert sentry_config["environment"]
    assert html.index("minds-sentry-config") < html.index("window.__MINDS_BOOTSTRAP__")


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


@pytest.mark.witnesses(
    "browser-authorization.no-data-without-session",
    partial="witnesses the app-status surface; the absence of user data across every route is universally quantified",
)
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
    assert isinstance(payload["workspace_count"], int)
    assert isinstance(payload["needs_error_reporting_consent"], bool)
    # No config store is wired here, so the install reads as past onboarding
    # (a minimal app must never route to the start flow).
    assert payload["is_onboarding_complete"] is True


def test_app_status_reports_onboarding_incomplete_for_a_fresh_install(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.get("/ui/api/app-status")

    payload = json.loads(response.get_data(as_text=True))
    assert payload["is_onboarding_complete"] is False
    assert payload["workspace_count"] == 0


def test_sanitize_accent_accepts_hex_and_rejects_anything_else() -> None:
    """The accent query param is inlined into the page's CSS custom property,
    so only plain hex colors may pass through."""
    assert _sanitize_accent("#aabbcc") == "#aabbcc"
    assert _sanitize_accent("#AB1") == "#AB1"
    assert _sanitize_accent(None) == DEFAULT_WORKSPACE_COLOR
    assert _sanitize_accent("") == DEFAULT_WORKSPACE_COLOR
    assert _sanitize_accent("url(https://evil.example/x)") == DEFAULT_WORKSPACE_COLOR
    assert _sanitize_accent("#aabbcc; background: red") == DEFAULT_WORKSPACE_COLOR


def test_a_windows_report_records_a_workspace_among_an_accounts_recents_only_when_it_is_that_accounts(
    tmp_path: Path,
) -> None:
    """A window moving to another account reports the workspace it is leaving under the account it is going to."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-alice", email="alice@example.com")
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)
    minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
    _client, app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        imbue_cloud_cli=cli,
        session_store=session_store,
        minds_config=minds_config,
    )
    alices_agent_id = str(AgentId.generate())
    session_store.associate_created_workspace(
        user_id="user-alice",
        agent_id=alices_agent_id,
        host_id=str(HostId.generate()),
        display_name="ws",
        color=None,
        is_cloud_row=False,
    )
    unassociated_agent_id = str(AgentId.generate())

    def report(account_id: str, workspace_agent_id: str | None, window_kind: UiWindowKind = UiWindowKind.MAIN) -> None:
        _record_recent_window_state(
            get_state(app),
            UiClientStateMessage(
                client_id="win",
                route="/",
                workspace_agent_id=workspace_agent_id,
                account_id=account_id,
                window_kind=window_kind,
            ),
        )

    report("user-alice", alices_agent_id)
    report("user-bob", alices_agent_id)
    report("local", unassociated_agent_id)
    report("user-alice", unassociated_agent_id)
    # A pulled-out window's report, and a window that has not named its account, count for nothing.
    report("user-carol", alices_agent_id, UiWindowKind.POPOUT)
    report("", alices_agent_id)
    # So does a report naming something that is not an account id: it is never persisted.
    report("../etc", alices_agent_id)

    assert minds_config.get_recent_account_ids() == ["user-alice", "local", "user-bob"]
    assert minds_config.get_recent_workspace_ids("user-alice") == [alices_agent_id]
    assert minds_config.get_recent_workspace_ids("user-bob") == []
    assert minds_config.get_recent_workspace_ids("local") == [unassociated_agent_id]
