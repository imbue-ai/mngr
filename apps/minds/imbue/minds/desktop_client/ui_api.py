"""The session-authed `/ui` surface: SPA index + `/ui/api/*` + `/ui/ws`.

This is the SPA's private backend, distinct from the agent-facing `/api/v1`
surface (latchkey-gated at the gateway) which stays untouched. Everything
here authenticates with the same signed session cookie the rest of the
desktop client uses.

- ``GET /ui/`` serves the SPA index page: hashed asset tags read from the
  Vite manifest plus the inlined ``window.__MINDS_BOOTSTRAP__`` document (the
  same publisher-built snapshot a fresh WebSocket receives, so first paint
  needs zero extra round trips).
- ``GET /ui/ws`` runs the channel connection (auth first, then handshake --
  see ``ui_channel.py``).
- ``/ui/api/<area>`` routes are registered by the per-area modules; each page
  tranche owns exactly one module, so parallel work never collides here.
"""

import json
import os
import re
from pathlib import Path

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger

from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_create import register_create_routes
from imbue.minds.desktop_client.ui_api_inbox import register_inbox_routes
from imbue.minds.desktop_client.ui_api_lifecycle import register_lifecycle_routes
from imbue.minds.desktop_client.ui_api_onboarding import register_onboarding_routes
from imbue.minds.desktop_client.ui_api_options import register_options_routes
from imbue.minds.desktop_client.ui_api_permissions import register_permissions_routes
from imbue.minds.desktop_client.ui_api_settings import register_settings_routes
from imbue.minds.desktop_client.ui_api_updates import register_update_routes
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_channel import run_ui_websocket_connection
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiBootstrap
from imbue.minds.desktop_client.ui_models import UiBootstrapSeed
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.errors import MindError
from imbue.minds.utils.sentry.core import resolve_anonymous_user_id
from imbue.minds.utils.sentry.frontend import frontend_sentry_browser_payload

_STATIC_UI_DIRECTORY: Path = Path(__file__).resolve().parent / "static" / "ui"
_VITE_MANIFEST_PATH: Path = _STATIC_UI_DIRECTORY / ".vite" / "manifest.json"

# Test/tooling override for the Vite manifest location (e.g. the visual-diff
# harness serving a scratch build); production always uses the packaged path.
_MANIFEST_PATH_ENV_VAR: str = "MINDS_UI_MANIFEST_PATH"


def _resolve_vite_manifest_path() -> Path:
    override = os.getenv(_MANIFEST_PATH_ENV_VAR)
    return Path(override) if override else _VITE_MANIFEST_PATH


_FRONTEND_NOT_BUILT_HTML: str = (
    "<html><body><p>Frontend not built. Run <code>pnpm install &amp;&amp; pnpm generate &amp;&amp; "
    "pnpm build</code> in <code>apps/minds/frontend/</code>.</p></body></html>"
)


class UiPublisherMissingError(MindError, RuntimeError):
    """Raised when the /ui surface is served by an app missing its publisher wiring."""

    ...


def read_vite_entry_tags() -> str | None:
    """Build the script/link tags for the built bundle, or None when it is not built.

    Reads the Vite manifest fresh per request: the dev loop rebuilds into the
    same directory and a stale cached manifest would serve hashed names that
    no longer exist.
    """
    manifest_path = _resolve_vite_manifest_path()
    try:
        manifest_raw = manifest_path.read_text()
    except FileNotFoundError:
        # The expected "frontend not built" state; the caller serves the 503.
        return None
    except OSError as e:
        # The manifest exists but could not be read: a real failure that must
        # not be silently collapsed into the not-built page.
        logger.warning("Could not read the Vite manifest at {}: {}", manifest_path, e)
        return None
    try:
        manifest = json.loads(manifest_raw)
    except json.JSONDecodeError as e:
        logger.warning("Ignoring an unparseable Vite manifest at {}: {}", manifest_path, e)
        return None
    tags: list[str] = []
    for entry in manifest.values():
        if not entry.get("isEntry"):
            continue
        for css_file in entry.get("css", ()):
            tags.append(f'<link rel="stylesheet" href="/_static/ui/{css_file}">')
        tags.append(f'<script type="module" src="/_static/ui/{entry["file"]}"></script>')
    if not tags:
        return None
    return "\n    ".join(tags)


# Accent colors are plain hex everywhere in the palette; anything else in the
# query string is discarded rather than inlined into the page's CSS.
_HEX_COLOR_PATTERN: re.Pattern[str] = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _sanitize_accent(raw_accent: str | None) -> str:
    """The accent query param constrained to a hex color (it feeds a CSS custom property)."""
    if raw_accent is not None and _HEX_COLOR_PATTERN.match(raw_accent):
        return raw_accent
    return DEFAULT_WORKSPACE_COLOR


def _build_bootstrap_json() -> str:
    """The serialized ``window.__MINDS_BOOTSTRAP__`` document for the current request."""
    state = get_state()
    publisher = state.ui_publisher
    if publisher is None:
        # create_desktop_client always wires a publisher; reaching this means
        # the app was constructed some other way, which is a bug.
        raise UiPublisherMissingError("The UI publisher was never wired into the app state")
    user_agent = request.headers.get("user-agent", "")
    seed = UiBootstrapSeed(
        accent=_sanitize_accent(request.args.get("accent")),
        is_mac="Macintosh" in user_agent or "Mac OS" in user_agent,
        # minds always runs the forward proxy with TLS, so the scheme is https.
        mngr_forward_origin=f"https://localhost:{state.mngr_forward_port or 8421}",
    )
    bootstrap = UiBootstrap(seed=seed, schema_version=UI_SCHEMA_VERSION, snapshot=publisher.build_snapshot())
    # "</" must not appear verbatim inside an inline <script> body: a name or
    # log line containing "</script>" would otherwise terminate the tag early.
    return bootstrap.model_dump_json().replace("</", "<\\/")


def _build_sentry_head_tags() -> str:
    """The browser Sentry bootstrap tags for the SPA index, or "" when reporting is off.

    Mirrors the old JinjaX ``Base`` layout: emitted only when the user's
    ``report_unexpected_errors`` setting is on and a real DSN is configured for
    the environment. The config rides as a JSON blob (not inline JS) that
    ``sentry_init.js`` reads; the bundle + init load synchronously in ``<head>``,
    before the SPA bundle, so early errors are captured.
    """
    minds_config = get_state().minds_config
    if minds_config is None:
        return ""
    is_error_reporting_enabled = minds_config.get_report_unexpected_errors()
    # Attach the install's stable anonymous user id (no PII) so browser events
    # count as the same install as the backend's in Sentry's per-issue user counts.
    anonymous_user_id = resolve_anonymous_user_id(minds_config.data_dir)
    sentry_payload = frontend_sentry_browser_payload(is_error_reporting_enabled, anonymous_user_id)
    if sentry_payload is None:
        return ""
    # "</" must not appear verbatim inside an inline <script> body (see the
    # bootstrap blob below for the same rule).
    payload_json = json.dumps(sentry_payload).replace("</", "<\\/")
    return (
        f'    <script type="application/json" id="minds-sentry-config">{payload_json}</script>\n'
        '    <script src="/_static/sentry.browser.min.js"></script>\n'
        '    <script src="/_static/sentry_init.js"></script>\n'
    )


def serve_spa_index(**_path_params: str) -> Response:
    """Serve the SPA index for any hub route.

    Registered both at ``/ui/`` and (by ``create_desktop_client``) at every
    page path the SPA router owns; the router reads the real
    ``location.pathname``, so the handler ignores path parameters. The embed
    contract module loads before the bundle because the shell consumes
    ``window.MindsEmbedContract`` at module-evaluation time.
    """
    if not is_ui_request_authenticated():
        return Response(status=302, headers={"Location": "/login"})
    entry_tags = read_vite_entry_tags()
    if entry_tags is None:
        return Response(_FRONTEND_NOT_BUILT_HTML, status=503, mimetype="text/html")
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "    <title>minds</title>\n"
        f"{_build_sentry_head_tags()}"
        f"    <script>window.__MINDS_BOOTSTRAP__ = {_build_bootstrap_json()};</script>\n"
        '    <script src="/_static/embed_contract.js"></script>\n'
        f"    {entry_tags}\n"
        "  </head>\n"
        '  <body><div id="app"></div></body>\n'
        "</html>\n"
    )
    return Response(html, mimetype="text/html")


def _handle_app_status() -> Response:
    """Startup probe for the Electron main process (and any headless caller).

    Auth-optional by design: the shell calls this before it knows whether a
    session exists. Unauthenticated callers learn only that they are not
    authenticated; workspace ids are disclosed only with a valid session.
    ``restorable_workspace_ids`` carries BOTH coordinates of each workspace
    (agent-keyed and host-keyed) because persisted window URLs are host-keyed
    while minds records are agent-keyed, and the shell's restore filter does
    plain membership checks.
    """
    if not is_ui_request_authenticated():
        return Response(
            json.dumps({"is_authenticated": False, "restorable_workspace_ids": []}), mimetype="application/json"
        )
    state = get_state()
    restorable_ids = [str(agent_id) for agent_id in state.backend_resolver.list_restorable_workspace_ids()] + [
        str(host_id) for host_id in state.backend_resolver.list_restorable_workspace_host_ids()
    ]
    minds_config = state.minds_config
    needs_consent = minds_config is not None and not minds_config.get_error_reporting_consent_given()
    session_store = state.session_store
    has_accounts = session_store is not None and len(session_store.list_accounts()) > 0
    payload = {
        "is_authenticated": True,
        "restorable_workspace_ids": restorable_ids,
        "needs_error_reporting_consent": needs_consent,
        # The Electron startup router's inputs (decideStartupRoute): whether
        # any imbue account is signed in and how many workspaces exist.
        "has_accounts": has_accounts,
        "workspace_count": len(state.backend_resolver.list_active_workspace_ids()),
    }
    return Response(json.dumps(payload), mimetype="application/json")


def _handle_ui_websocket() -> Response:
    # Auth runs BEFORE the WebSocket handshake: rejection is a plain HTTP 401
    # on an intact connection (no socket hijack, no gateway suppression).
    if not is_ui_request_authenticated():
        return Response(json.dumps({"error": "authentication required"}), status=401, mimetype="application/json")
    state = get_state()
    publisher = state.ui_publisher
    if publisher is None:
        raise UiPublisherMissingError("The UI publisher was never wired into the app state")
    return run_ui_websocket_connection(
        broadcaster=state.ui_channel_broadcaster,
        # Passed as a callable: the connection derives the snapshot only after
        # registering with the broadcaster, so no concurrent publish is lost.
        build_snapshot_frames=publisher.build_snapshot_frames,
    )


def create_ui_blueprint() -> Blueprint:
    """Assemble the `/ui` blueprint: index, channel, and the per-area route groups."""
    blueprint = Blueprint("ui", __name__, url_prefix="/ui")
    blueprint.add_url_rule("/", view_func=serve_spa_index)
    blueprint.add_url_rule("/api/app-status", view_func=_handle_app_status)
    # websocket=True is required for werkzeug's router to match upgrade
    # requests to this rule (they raise WebsocketMismatch -> 400 otherwise).
    blueprint.add_url_rule("/ws", view_func=_handle_ui_websocket, websocket=True)
    register_create_routes(blueprint)
    register_settings_routes(blueprint)
    register_options_routes(blueprint)
    register_permissions_routes(blueprint)
    register_lifecycle_routes(blueprint)
    register_inbox_routes(blueprint)
    register_onboarding_routes(blueprint)
    register_update_routes(blueprint)
    return blueprint
