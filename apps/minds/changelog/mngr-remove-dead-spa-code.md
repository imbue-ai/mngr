Removed the dead pre-SPA front end of the minds desktop client. The Mithril SPA (`apps/minds/frontend/`) has owned every page since the web-only merge; this deletes the unused JinjaX rendering layer (`desktop_client/templates/`, `templates.py`) and all template-only static assets (the legacy per-page vanilla JS, the Tailwind `app.css` and its entire build chain, `latchkey_logo.png`), along with the `jinja2`/`jinjax` dependencies. `static/` now holds only the embed contract module, the Sentry browser bundle + init, the service icons, and the built SPA bundle.

Live logic that had been living in `templates.py` moved to dedicated modules: `workspace_defaults.py` (template repo URL/ref defaults, including the release-pinned `FALLBACK_BRANCH`), `host_names.py` (host-name slug/`workspace-N` derivation), `create_status.py` (create-attempt captions and progress durations), and `data_types.py` (`RemoteWorkspaceTile`). The release docs now point at `workspace_defaults.py` for the `FALLBACK_BRANCH` bump.

Browser-side Sentry error reporting now works in the SPA: the index page emits the same Sentry bootstrap the deleted JinjaX layout used to (gated on the user's error-reporting setting). It had been silently absent since the SPA migration.

The few remaining server-rendered responses are now dependency-free static documents (`static_pages.py`): the invalid one-time-code page and the friendly 404/405 navigation page; the unauthenticated `/welcome/skip` request now redirects to `/login`.

The inbox's HTML-fragment path is gone: request handlers only expose their typed `build_request_detail_payload` (the SPA renders the dialogs), and the latchkey dialog vocabulary moved from `latchkey/handlers/templates.py` to `latchkey/handlers/account_choices.py`.

The legacy `ChromeEventBroadcaster` (the fan-out for the deleted `/_chrome/events` SSE) was removed; producers publish `workspace_stopped` / `open_help` / `workspace_refresh` frames directly onto the `/ui/ws` channel via the UI state publisher.

`scripts/visual_diff.py` lost its JinjaX `capture` mode; `capture-spa` and `compare` remain.

`app.py` also shed the helpers the SPA route flip had orphaned: the legacy `POST /workspaces/destroyed/<agent_id>/delete-backup` form route (the SPA calls the `/ui/api/destroyed-workspaces/<agent_id>/delete-backup` twin) and a set of zero-reference private helpers whose live successors are the `ui_api_*` modules. The unit tests that had pinned the dead copies now target the live twins (the recovery SSH-command builder and workspace-coordinate resolver in `ui_api_lifecycle.py`, the host-coordinate helper in `ui_api_options.py`).

`scripts/download-binaries.js` now retries failed downloads harder (8 attempts with backoff capped at 30s, up from 5 attempts / ~15s total): GitHub's release CDN resets connections for minute-plus windows from cloud-builder egress, which made the minds e2e Modal image bake fail on the dugite-native download once this branch's build-step change invalidated the cached layer.
