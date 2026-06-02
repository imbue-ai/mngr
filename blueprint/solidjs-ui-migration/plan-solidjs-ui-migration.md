# Solid.js UI migration

Replace the Jinja2 + vanilla-JS desktop UI under `apps/minds/imbue/minds/desktop_client/` with a Solid.js application served via SSR from a Node sidecar, driven by SSE for live state. Done page-by-page with a parity-gate testing policy.

## Overview

- Drop Jinja2 from the desktop client entirely; replace ~20 templates and ~1300 lines of vanilla JS with Solid.js components rendered server-side from a Node sidecar.
- Keep FastAPI as the front door — it continues to own routing, auth cookies, JSON/SSE endpoints, and now proxies HTML routes to a Node-side `solid-start` SSR process.
- Standardize on one global SSE stream (`/_chrome/events`, extended) as the single source of UI truth; Solid components subscribe to slices via a shared store.
- Add Vite + Tailwind v4 as the new build pipeline (JavaScript only — no TypeScript); drop the runtime Tailwind CDN.
- Land all infrastructure on one trivial page first (`/welcome`), then migrate page-by-page under a per-page parity gate: each Jinja removal must ship with a Solid component test and a JSON/SSE contract test in the same commit.
- Use `solid-router` for client-side navigation between content pages; chrome and sidebar keep being separate Electron `WebContentsView` bundles, each with its own SSR entry.

## Expected behavior

### User-visible

- Pages look and behave the same as today; the migration is structural, not visual.
- First paint is server-rendered HTML (via SSR), then Solid hydrates — no flash of empty content compared to a pure SPA.
- Client-side navigation between content pages (e.g. landing → create → creating) becomes instant after first paint; chrome remains stable across navigations.
- Form submissions on `/create`, `/accounts/*`, `/sharing/.../{enable,disable}`, `/workspace/.../{associate,disassociate}`, `/requests/{id}/{grant,deny}` switch from form-encoded POSTs that re-render HTML to JSON `fetch` calls; errors render inline via component state instead of a page reload.
- Live state (workspaces list, auth status, pending requests, providers panel, agent creation status, destroy log tail) all flow through one extended `/_chrome/events` SSE stream; per-page SSE endpoints stay as today only where they carry binary log data (`/api/create-agent/{id}/logs`, `/api/destroying/{id}/log`).
- If the SSR sidecar is unhealthy, FastAPI returns a minimal shell HTML that mounts the Solid bundle client-side; the page still loads, just without SSR's first-paint benefit.

### System-level

- A new long-running Node process (the SSR sidecar) is part of `minds run`, supervised by FastAPI via a `ConcurrencyGroup` task with crash-restart.
- The sidecar runs under the Electron binary as Node (`ELECTRON_RUN_AS_NODE=1`) in the packaged app, and under a Node found on `PATH` in dev (`uv run minds run`). Mirrors the existing latchkey CLI bundle pattern.
- `/_static/` now serves Vite's compiled bundle in production; in dev mode FastAPI proxies `/_static/*` to the Vite dev server (HMR works inside Electron).
- `templates_test.py` is deleted incrementally as templates are removed; Vitest replaces it as the per-page unit test layer.
- `apps/minds/package.json` gains Solid, Vite, Tailwind v4 dependencies; `scripts/build.js` learns to build and embed the Solid bundle into `resources/`.

## Implementation plan

### New: Solid frontend project

Layout under `apps/minds/frontend/`:

- `package.json` — declares `solid-js`, `@solidjs/router`, `@solidjs/start`, `vite`, `vite-plugin-solid`, `tailwindcss@4`, `@tailwindcss/vite`, `vitest`, `@solidjs/testing-library`, `@testing-library/jest-dom`, `jsdom`.
- `vite.config.js` — three SSR entries: `chrome`, `sidebar`, `app` (the content bundle that owns landing/create/creating/destroying/workspace_settings/sharing/accounts/recovery/login/welcome/auth_error/login_redirect). Output dir `dist/`. Tailwind plugin enabled. Solid-start SSR adapter configured for Node target with manual mount points (not file-routing).
- `src/main/chrome.entry.jsx` — SSR + hydrate entry for chrome shell; mounts `<ChromeApp />`.
- `src/main/sidebar.entry.jsx` — SSR + hydrate entry for sidebar; mounts `<SidebarApp />`.
- `src/main/app.entry.jsx` — SSR + hydrate entry for content bundle; mounts `<ContentApp />` which contains the solid-router route tree.
- `src/main/server.js` — minimal Node sidecar HTTP server that exposes `POST /__ssr/<bundle>` (or `GET /<route>?bundle=<bundle>`) accepting initial state JSON, returning rendered HTML for that route. Reads the manifest from `dist/`.
- `src/routes/` — one Solid component per route:
  - `landing.jsx`, `create.jsx`, `creating.jsx`, `destroying.jsx`
  - `workspace_settings.jsx`, `sharing.jsx`, `accounts.jsx`, `recovery.jsx`
  - `login.jsx`, `welcome.jsx`, `auth_error.jsx`, `login_redirect.jsx`
  - `auth/signup_signin.jsx`, `auth/forgot_password.jsx`, `auth/check_email.jsx`, `auth/oauth_close.jsx`, `auth/settings.jsx`
  - `permissions/predefined.jsx`, `permissions/file_sharing.jsx`, `permissions/index.jsx`
- `src/components/ui/` — Solid component library porting `_macros.html`: `Button.jsx`, `ButtonLink.jsx`, `Card.jsx`, `CardRow.jsx`, `TextInput.jsx`, `Notice.jsx`, `Spinner.jsx`, `PageContainer.jsx`, `OptionCard.jsx`.
- `src/components/chrome/` — `Titlebar.jsx`, `Sidebar.jsx`, `RequestsPanel.jsx`, `ProvidersPanel.jsx`.
- `src/components/creating/` — `OnboardingQuestion.jsx`, `LoadingScreen.jsx`, `LogStream.jsx`.
- `src/lib/workspace_accent.js` — pure helper, OKLCH hue derivation from `agent_id` (mirrors today's `workspace_accent.js` and the Python `workspace_accent()`).
- `src/lib/sse_store.js` — exports `createSseStore(url)`; opens `EventSource(url)`, owns a `createStore` from `solid-js/store`, applies typed envelope events from `/_chrome/events` to slices of the store. Exposes a Solid context (`SseStoreProvider`) and selector hook (`useSseSlice(path)`).
- `src/lib/api.js` — typed `fetch` wrappers for every JSON endpoint (mirroring the FastAPI handlers); returns `{ ok, errors, data }`.
- `src/lib/electron_bridge.js` — wraps `window.electron` IPC calls (workspace open, focus, navigate) behind a thin interface that no-ops in browser/dev.
- `src/styles/globals.css` — Tailwind v4 `@import "tailwindcss"`, a single `@theme` block carrying the tokens previously in `tokens.css`, plus a small handful of global selectors (`.spinner`, `.opt`, `.opt-selected`, `.accent-spine`) that don't naturally fit Tailwind utilities.
- `src/test/setup.js` — Vitest setup; loads `@testing-library/jest-dom` matchers.
- `src/routes/*.test.jsx` — Vitest unit tests per route; render the component with mock initial state and assert on user-visible structure (replaces `templates_test.py` assertions).
- `src/components/ui/*.test.jsx` — Vitest unit tests for each primitive.
- `src/lib/sse_store.test.js` — tests for the store reducer (envelope → state) using a mock EventSource.

### Modified: Python desktop client

- `apps/minds/imbue/minds/desktop_client/ssr_sidecar.py` (new) — `SsrSidecar` class supervised inside the existing `ConcurrencyGroup` task graph. Owns:
  - `start(self) -> None` — spawns the Node process (`MINDS_ELECTRON_EXEC_PATH` with `ELECTRON_RUN_AS_NODE=1` in packaged builds, `node` on PATH in dev), pipes stdout/stderr into loguru.
  - `wait_ready(self, timeout: float = 10.0) -> None` — polls a `GET /__ssr/health` on the sidecar.
  - `is_healthy(self) -> bool` — cheap probe used by the proxy fallback.
  - `render(self, *, bundle: str, route: str, initial_state: Mapping[str, Any]) -> str` — POSTs to the sidecar, returns rendered HTML.
  - Restart-on-crash policy with exponential backoff capped at 5s; emits a structured warn after 3 consecutive failures.
- `apps/minds/imbue/minds/desktop_client/templates.py` — gutted in lockstep with the migration. Each `render_*` function becomes a thin shim that:
  - Builds an `initial_state` dict (the same kwargs Jinja used today, but as JSON-serializable data).
  - Calls `SsrSidecar.render(bundle=..., route=..., initial_state=...)`.
  - On unhealthy sidecar, returns a fallback shell HTML inlined in `templates.py` that loads `/_static/<bundle>.js` and embeds `initial_state` in a `<script type="application/json" id="__initial_state__">` blob for client-side hydration.
  - The module shrinks to a small ~60-line file once Jinja goes; eventually the `render_*` functions are deleted in favor of inline `Response(ssr.render(...))` calls in `app.py`, but they stay as a per-page seam during the migration to minimize churn in `app.py` routes.
- `apps/minds/imbue/minds/desktop_client/templates_auth.py` — same migration treatment as `templates.py`.
- `apps/minds/imbue/minds/desktop_client/app.py`:
  - Construct an `SsrSidecar` at startup, register it in the `ConcurrencyGroup`, expose it via `app.state.ssr` for route handlers.
  - HTML route handlers (e.g. `_handle_landing_page`, `_handle_create_page`, `_handle_creating_page`) keep their signatures; their body just shifts from `render_landing_page(...)` to `app.state.ssr.render(bundle="app", route="/", initial_state={...})`.
  - Convert form-post handlers (`_handle_create_form_submit`, `_handle_workspace_associate`, `_handle_sharing_enable`, `_handle_account_logout`, etc.) to return JSON `{ ok: bool, errors: {field: msg}, data?: any }` instead of re-rendered HTML; the Solid form components fetch these endpoints and render errors inline.
  - Extend `/_chrome/events` envelope set to carry: per-page state (create form defaults, creating-status, destroying-status, accounts state, workspace_settings state, sharing state, providers state). Today's narrow set (workspaces, auth, requestCount, requestIds) becomes the chrome slice of a larger envelope schema. Events are keyed by `{ topic, payload }`; existing event topics stay backward-compatible.
  - Add a `/_static/__manifest__.json` route that returns Vite's manifest so the SSR sidecar can resolve hashed asset filenames.
  - Dev-mode `/_static/*` proxy: when `MINDS_VITE_DEV_URL` is set, FastAPI proxies `/_static/*` requests to that URL (Vite dev server with HMR). In prod, FastAPI serves the static `dist/` files via the existing `StaticFiles` mount.
- `apps/minds/imbue/minds/desktop_client/static/` — emptied as templates migrate. `tailwind.js` and the hand-written `*.js` files are deleted in lockstep with their consumers. `workspace_accent.js` is the last to go (it has callers in `chrome.js` and `sidebar.js`).
- `apps/minds/imbue/minds/desktop_client/static/tokens.css` — content moves into `frontend/src/styles/globals.css` and `@theme` config; file deleted at the end of the migration.
- `apps/minds/imbue/minds/desktop_client/api_v1.py` — receives any new JSON endpoints required by the form-post conversion (most of these endpoints already exist for the `/api/*` paths; the rest are added one per migrated form).

### Modified: build, packaging, dev tooling

- `apps/minds/package.json` — add Solid/Vite/Tailwind/Vitest devDependencies; add `frontend:dev`, `frontend:build`, `frontend:test`, `frontend:test:unit` scripts. Drop the `fetch-tailwind` postinstall and the `fetch_tailwind.sh` script.
- `apps/minds/scripts/build.js` — new `bundleFrontend()` step:
  - Runs `pnpm --filter ./frontend build` (or equivalent within the monorepo's pnpm workspace).
  - Copies `frontend/dist/` into `resources/frontend/` (server bundle for the sidecar) and into `apps/minds/imbue/minds/desktop_client/static/` (client assets served by FastAPI).
  - Bundles the SSR server entry plus its `node_modules` subtree into `resources/frontend/server/`, with a `bin/ssr-sidecar` shim mirroring the existing latchkey shim (Electron-as-Node).
- `apps/minds/scripts/fetch_tailwind.sh` — deleted.
- `apps/minds/electron/main.js` — unchanged in behavior; the FastAPI startup is the same entry point. (Sidecar lifecycle stays on FastAPI's side per Q20a, so Electron doesn't need to know about it.)
- `justfile` — add `just minds-frontend` (alias for `frontend:dev`) and `just minds-frontend-build` recipes.
- `apps/minds/pyproject.toml` — add `httpx` to runtime deps if not present (used by the sidecar proxy / probe); add `uvicorn[standard]` if WebSocket proxying becomes necessary (not required for SSE, but useful for the Vite proxy).

### Data type sketches

- `frontend/src/lib/sse_envelope.js` documents the envelope shapes (workspaces, auth, requests, providers, create_form, creation_status, destroying_status, accounts, workspace_settings, sharing) as JSDoc typedefs.
- Python side: `desktop_client/sse_envelope.py` (new) holds the matching `pydantic` models so endpoint code can build envelopes type-checked. The two sides stay in sync via a hand-written JSDoc mirror; if drift becomes painful, switch to a `datamodel-code-generator` step (deferred — see Open questions).

## Implementation phases

Each phase is independently shippable and leaves the system working. The full migration is finished only at the end of Phase 8; Phase 1–2 add infrastructure without removing Jinja.

### Phase 1 — Infrastructure spike on `/welcome` (no Jinja removed yet)

- Scaffold `apps/minds/frontend/` with `package.json`, `vite.config.js`, Tailwind v4, Vitest.
- Build `src/components/ui/` (component library port of `_macros.html`).
- Build `src/lib/workspace_accent.js`, `src/lib/sse_store.js`, `src/lib/api.js`, `src/lib/electron_bridge.js`.
- Build the minimal `src/main/server.js` SSR sidecar and one Solid route: `src/routes/welcome.jsx`.
- Add `ssr_sidecar.py` and wire it into FastAPI startup; supervised by `ConcurrencyGroup`.
- Add the Vite dev proxy + manifest endpoint in `app.py`.
- Add `scripts/build.js` frontend bundling and the Electron-as-Node shim for the sidecar.
- Make `_handle_welcome_page` use the SSR sidecar (with the inline-shell fallback path).
- Tests: Vitest setup, one component test for `welcome.jsx`, one Python integration test that asserts the SSR sidecar renders `/welcome` end-to-end.

End state: `/welcome` is the first Solid page; everything else still runs on Jinja. Both serving modes coexist.

### Phase 2 — Drop the Tailwind CDN; migrate trivial static pages

- Migrate `auth_error`, `login`, `login_redirect` routes.
- Replace the runtime Tailwind CDN with the Vite-built CSS in `globals.css`; delete `fetch_tailwind.sh` and the postinstall hook.
- Confirm `pnpm build` produces a fully self-contained `dist/` (no network at runtime).

End state: trivial pages on Solid; CDN gone; Tailwind v4 token theme owns design tokens.

### Phase 3 — Chrome and sidebar bundles

- Migrate `chrome.html` + `chrome.js` → `chrome.entry.jsx` + `components/chrome/*`.
- Migrate `sidebar.html` + `sidebar.js` → `sidebar.entry.jsx` + `Sidebar.jsx`.
- Wire the SSE store to `/_chrome/events`; replace the hand-rolled DOM update code in `chrome.js`.
- Wire IPC bridge calls via `electron_bridge.js`.

End state: the persistent shell is Solid. Content area still loads Jinja-rendered content per-iframe URL.

### Phase 4 — Landing + create flow

- Migrate `landing.html` → `routes/landing.jsx`. Extend `/_chrome/events` to carry per-workspace destroying/backup/health badges.
- Migrate `create.html` + the create form's per-field validation; convert `POST /create` to return JSON `{ ok, errors }`.
- Migrate `creating.html` + `creating.js` → `routes/creating.jsx` + `components/creating/*`. Reuse the existing `/api/create-agent/{id}/logs` SSE channel (it carries binary log data; stays as today).

End state: workspace creation flow runs entirely on Solid. Per-page parity tests in place for landing + create + creating.

### Phase 5 — Destroying, recovery, accounts

- Migrate `destroying.html` + `destroying.js` → `routes/destroying.jsx`. Keep `/api/destroying/{id}/log` as today (binary log tail).
- Migrate `recovery.html` → `routes/recovery.jsx`.
- Migrate `accounts.html` → `routes/accounts.jsx`; convert `POST /accounts/set-default` and `POST /accounts/{user_id}/logout` to JSON.

### Phase 6 — Workspace settings + sharing

- Migrate `workspace_settings.html` + `workspace_settings.js` → `routes/workspace_settings.jsx`; convert associate/disassociate posts to JSON.
- Migrate `sharing.html` + `sharing.js` → `routes/sharing.jsx`; convert sharing enable/disable to JSON.

### Phase 7 — Auth pages and permissions

- Migrate `templates/auth/*.html` → `routes/auth/*.jsx` (signup_signin, forgot_password, check_email, oauth_close, settings) via `templates_auth.py`.
- Migrate `permissions.html`, `latchkey_predefined_permission.html`, `latchkey_file_sharing_permission.html` → `routes/permissions/*`.
- Delete `templates_test.py`, `templates_auth.py` Jinja code, `static/auth.js`, and any remaining vanilla JS.

### Phase 8 — Cleanup

- Delete `templates/` directory entirely.
- Delete `JINJA_ENV` and the Jinja import from `templates.py`; remove the dependency from `pyproject.toml`.
- Tighten the fallback path: if the sidecar is unhealthy on production for >N seconds, surface a structured error notification via `NotificationDispatcher`.
- Remove the per-page registry of "migrated" routes (everything is migrated; the registry is no longer load-bearing).
- Bump ratchet thresholds in `apps/minds/imbue/minds/test_ratchets.py` so the Jinja-related counts reflect the new (zero) state.

## Testing strategy

### Per-page parity gate (CI-enforced)

For every PR that removes a Jinja template:

- A matching Solid component test exists under `frontend/src/routes/<name>.test.jsx` or `frontend/src/components/.../*.test.jsx` and asserts on the same observable behavior the Jinja unit test did (form labels visible, options listed, error messages rendered, hidden/visible state transitions on user interaction).
- A Python test asserts the JSON/SSE contract the route depends on — either a new test in `test_desktop_client.py` for the JSON endpoint, or an envelope-shape test in a new `sse_envelope_test.py`.
- The corresponding `templates_test.py` test is deleted in the same commit; CI fails if the Jinja file is gone but its test isn't.

### Unit tests

- `frontend/src/components/ui/*.test.jsx` — every UI primitive (button, card, notice, spinner, page container, option card, text input) tested via `@solidjs/testing-library`. Assert on user-visible structure, not internal class names.
- `frontend/src/routes/*.test.jsx` — each route's happy path + at least one error state, rendered with mock initial state.
- `frontend/src/lib/sse_store.test.js` — feeds canned envelopes through a mock `EventSource` and asserts the resulting store state.
- `frontend/src/lib/api.test.js` — wraps `fetch` with a mock and asserts request bodies and response handling for each form-post conversion.
- `frontend/src/lib/workspace_accent.test.js` — golden test that mirrors the Python `workspace_accent` test; same input agent ids must produce identical OKLCH strings to keep the chrome and Electron stripes consistent with the Python side.
- Python side: replace `templates_test.py` cases with same-shape tests against the new JSON endpoints. Where the original test asserted "this label appears in the HTML", the new Python test asserts "the JSON response contains this label as the appropriate field"; the Vitest counterpart asserts "the Solid component renders that label".

### Integration tests

- New `test_ssr_sidecar.py` — boots the FastAPI app with the Node sidecar enabled, hits `/welcome`, `/`, `/create`, asserts SSR HTML contains both the server-rendered content and the hydration script tag pointing at the right manifest entry.
- New `test_ssr_fallback.py` — boots FastAPI with the sidecar deliberately disabled, asserts the fallback shell HTML is returned and contains the inlined `initial_state` JSON blob.
- Existing `test_desktop_client.py` — assertions on HTML text continue to work (SSR returns full HTML); update only where text changes. Existing `test_sse_redirect.py` is unaffected.

### Acceptance tests

- New `@pytest.mark.acceptance` Playwright test under `apps/minds/test_frontend_smoke.py` — launches the packaged app (or `uv run minds run`), navigates landing → create form → creating page → landing, asserts each page renders and the SSE store updates the workspaces list after creation.
- Existing acceptance tests continue to run; they exercise FastAPI routes, which now go through SSR end-to-end.

### Edge cases to cover

- Sidecar crashes mid-render: assert the FastAPI request falls back to the client-render shell (no 5xx surfaced to Electron).
- Sidecar slow to start: the dev mode start-up grace period must let `uv run minds run` come up cleanly within `wait_ready`'s timeout.
- Stale Vite manifest: a `pnpm build` left half-finished must not produce HTML that references nonexistent hashed assets — the build script writes the manifest atomically.
- SSE reconnect race: when the global stream reconnects, the SSE store must replace state with the server's snapshot envelope rather than merging onto stale data.
- Form submission while disconnected: JSON form posts must surface a clear "offline" error state in the Solid component, mirroring today's behavior where the form just re-renders with a connection error.

## Open questions

- **Solid-start vs. a hand-rolled SSR loop**: the plan commits to "solid-start's SSR primitives only" (Q13b). If solid-start's adapter assumptions push back hard against a sidecar-with-FastAPI-in-front model, we may need to drop solid-start entirely (Q13c — `solid-js/web`'s `renderToStringAsync` from a hand-rolled Node sidecar) and accept more wiring. Decide during Phase 1 once the welcome page is bootstrapping.
- **SSE envelope schema drift**: Python `pydantic` models and JS JSDoc types must stay in sync. Hand-mirroring is fine for ~10 envelope topics; if it grows past ~20, add a `datamodel-code-generator` or `quicktype` step. Not worth automating up front.
- **Node runtime in dev mode**: dev uses `node` from PATH. Pinning a Node version becomes a contributor-setup concern. Options: document it in `apps/minds/README.md`, add a check in `ssr_sidecar.py` startup that warns on `< 20`, or vendor a Node binary even in dev (heavy).
- **Routing for chrome ↔ content split**: chrome and sidebar are separate Electron `WebContentsView`s; cross-view navigation goes through Electron IPC, not solid-router. Confirm the IPC bridge in `electron_bridge.js` is the only place that touches `window.electron`, so the Solid components stay testable in jsdom.
- **Existing `chrome.html` data-attributes**: the current chrome page exposes `data-mngr-forward-origin`, `data-landing-agent-ids`, etc. The new SSR shell can carry the same hooks, but it's worth deciding whether to retire those attributes in favor of a single inlined `initial_state` JSON blob, which is cleaner but changes the public-ish surface that Electron's preload reads.
- **Pure-render assertions**: `templates.py`'s `@pure` decorator currently guarantees `render_*` functions are deterministic and side-effect-free. With SSR going through a Node sidecar, purity is no longer enforceable at the Python boundary. Decide whether to drop `@pure`, keep it on the thin shims (it still applies to the Python-side argument marshaling), or move equivalent assertions into the Vitest layer.
- **What happens to `templates_test.py` during Phase 3–7**: the parity gate deletes one Jinja test per migrated page. If a Phase covers multiple pages in one PR, the test file shrinks one entry at a time within that PR. Acceptable, but worth confirming that the ratchet check doesn't fire mid-PR.
- **Form posts that still want HTML responses**: any external integration (e.g. SuperTokens redirect flows) that posts to `/create` or similar and expects an HTML response would break under the JSON conversion. Audit `supertokens_routes.py` and any third-party callers before Phase 4 lands.
- **Bundle weight**: three SSR bundles (chrome, sidebar, app) each ship Solid's runtime. Worth measuring after Phase 3 — if total weight is uncomfortable for the Electron startup time, consolidate into a single `app` bundle that handles chrome + sidebar + content via solid-router (Q8c, deferred).
