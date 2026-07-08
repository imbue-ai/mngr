# Plan: Minds API route consolidation

> **Consolidate the leftover minds desktop-client UI routes onto a single, consistently-authed `/api/v1` surface that speaks only "workspace" (plus "desktop"/"provider" for app-level bits), never "agent"/"host"/"system-interface".**
> * One auth implementation for *every* `/api/v1` route (cookie **or** central bearer); agent reachability is decided solely by whether a `minds-workspaces-<verb>` schema matches the path at the latchkey gateway. Collapse today's split (`require_minds_api_key` on notifications/report vs `require_api_or_cookie_auth` on workspaces) into one helper used everywhere.
> * API vocabulary is "workspace" only; operations that differ by layer take a `scope` parameter rather than leaking host/agent into URLs.
> * Three new **per-target** `minds-workspaces` verbs — `-update`, `-recover`, `-sharing` (none/all/selected, like destroy/lifecycle/ssh); existing verbs unchanged.
> * Every leftover UI route gets a v1 home (or is dropped in favor of client-side fan-out); the browser create flow moves onto the **same** `POST /api/v1/workspaces` agents use, so the two create paths can't drift.
> * Old `app.py` routes are removed per-flow once repointed and Electron-verified — no long-lived deprecated aliases.
> * **In scope:** the create-flow repoint (browser still polls the dead `/api/create-agent/<id>/status|logs`). **Out of scope:** the SSH remote→local broker + `prune_expired_grant_lines` wiring (handoff #5), tracked separately.

## Overview

- Today the minds desktop client serves a mix of versioned `/api/v1/workspaces/...` routes (cookie-or-bearer, agent-reachable) and a pile of ad-hoc UI-only JSON routes in `app.py` (`/api/backup-status`, `/api/workspaces/<id>/color`, `/api/providers/<name>/toggle`, recovery, `/api/minds/*`, sharing, create status/logs). This makes auth inconsistent and lets the UI and agent surfaces drift.
- The goal is one HTTP surface with one auth implementation, organized into three buckets: `/api/v1/workspaces/...` (workspace management, per-verb agent-gateable), `/api/v1/desktop/...` (install-scoped app lifecycle + provider config, cookie-only), and the existing `/api/v1/agents/<id>/...` (per-agent self-calls).
- Auth is unified so we get it right once: anything on the API *could* be exposed to agents, but exposure is an additive, per-route decision (mint a verb) — the deny-all gateway baseline blocks any path without a matching verb.
- The minds API speaks only in workspaces (the minds-level abstraction over an mngr host + its `system-services` agent). Layer differences (restart the services agent vs the whole host) become a `scope` parameter, not separate agent/host URLs.
- The browser create flow is the last real migration: it moves onto `POST /api/v1/workspaces` (and the v1 operations resource for status/logs), retiring the parallel `/create` POST + `/api/create-agent/<id>/status|logs` path so app and agent creates share one implementation.
- Permission model is unchanged in mechanism (the `minds-workspaces` scope, grant-carried schemas merged by name — see `apps/minds/docs/latchkey-permissions.md`); this work only adds three verbs to the catalog.

## Expected behavior

- **No user-visible change to the browser UI** beyond the create error path: pages, flows, and outcomes are identical. The create form's validation errors are now rendered by the page JS from a structured JSON response instead of a server-side HTML re-render, but the inline-error UX (messages next to fields, preserved input) is preserved.
- **Backups badges:** the landing page issues one `GET /api/v1/workspaces/<id>/backups` per workspace (instead of the batch route) and reads each tile's `created_at` from the workspaces list, so a freshly-created workspace still shows "Created N ago" rather than "No backups".
- **Workspace settings:** changing color or account association issues `PATCH /api/v1/workspaces/<id>`; disassociating an account is `PATCH` with `account_id: null`.
- **Recovery:** the recovery page reads `GET /api/v1/workspaces/<id>/health` and triggers `POST /api/v1/workspaces/<id>/restart` with `scope: services | host`. A restart returns an operation handle and is followed via `/api/v1/workspaces/operations/<id>` (+`/logs`), exactly like create/destroy.
- **Providers:** enabling/disabling a provider is `PATCH /api/v1/desktop/providers/<name>` with `{enabled}`. Disabling a provider that still has active workspaces is rejected with an error (the call fails; nothing is changed).
- **Desktop quit flow:** the Electron quit path calls the three `/api/v1/desktop/...` routes (running-workspaces, bulk stop-hosts, state-container stop) with identical behavior to today.
- **Create:** submitting the create form drives `POST /api/v1/workspaces`; `/creating/<id>` polls the v1 operations resource for status + logs. Behavior (auto-naming, account/region/backup handling, redirect into the workspace) is unchanged.
- **Agents:** once granted, an agent can update (recolor/re-associate), recover (health + restart), and toggle sharing on a *selected* peer workspace via the new per-target verbs. Desktop and provider routes have no verb, so agents are blocked at the gateway and cannot reach them.
- **Dismiss:** dismissing a finished destroy card issues `DELETE /api/v1/workspaces/operations/<id>`; the destroy record is now in-memory, so an app restart mid-destroy drops the card (the detached `mngr destroy` keeps running and the workspace still disappears from discovery) — matching how create already behaves.

## Changes

**Unified auth (`apps/minds/imbue/minds/desktop_client/api_v1.py`)**

- Replace the two-decorator split with a single caller-resolution helper applied to every `/api/v1` route (resolves identity from session cookie or the central `MINDS_API_KEY` bearer); notifications/report adopt it too. Agent gating stays at the gateway via verb-to-path matching.

**New `/api/v1/workspaces/<id>` routes (`api_v1.py`)**

- `PATCH /api/v1/workspaces/<id>` — partial workspace metadata (color, account association; `account_id: null` disassociates). Replaces `/api/workspaces/<id>/color` and the `/workspace/<id>/associate|disassociate` POSTs. Gated by `minds-workspaces-update`.
- `GET /api/v1/workspaces/<id>/health` + `POST /api/v1/workspaces/<id>/restart` (`scope: services|host`); restart returns an operation handle (op id = workspace id, `kind: restart`). Replaces `/api/agents/<id>/host-health`, `/restart-system-interface`, `/restart-host`. Gated by `minds-workspaces-recover`.
- `GET .../sharing/<service>`, `GET .../sharing/<service>/readiness`, `PUT .../sharing/<service>` (body = emails), `DELETE .../sharing/<service>`. Replaces `/api/sharing-status|readiness/...` and `/sharing/<id>/<svc>/enable|disable`. Gated by `minds-workspaces-sharing`.
- `DELETE /api/v1/workspaces/operations/<id>` — replaces `/api/destroying/<id>/dismiss`.

**New `/api/v1/desktop/...` routes (cookie-only, no verb) (`api_v1.py`)**

- `PATCH /api/v1/desktop/providers/<name>` `{enabled}` (idempotent; rejects disabling a provider with active workspaces). Replaces `/api/providers/<name>/toggle`.
- Three distinct routes replacing `/api/minds/running`, `/api/minds/stop-hosts`, `/api/minds/stop-state-container` (running-workspaces; bulk stop-hosts; state-container stop).

**Create path (`api_v1.py`, `app.py`, `static/creating.js`, `templates/pages/Create.jinja`)**

- Browser create posts to `POST /api/v1/workspaces`; `_handle_create_workspace` returns structured JSON validation errors (field + message) for the page JS to render inline.
- `/creating/<id>` (`creating.js`) polls `/api/v1/workspaces/operations/<id>` (+`/logs`).
- Remove the old `/create` POST handler (`_handle_create_form_submit`) and the now-dead `/api/create-agent/<id>/status` + `/api/create-agent/<id>/logs` routes. The `GET /create` form page stays.

**Removed / fanned-out (`app.py`, `templates/pages/Landing.jinja`)**

- Remove batch `/api/backup-status`; landing JS fans out per-workspace `GET /api/v1/workspaces/<id>/backups` and reads `created_at` from the workspaces list (per-workspace backups response untouched).

**Destroy state (`destroying.py`, `api_v1.py`)**

- Move the destroy operation record from on-disk (`<data_dir>/destroying/<id>/`) to in-memory server state, consistent with create; the operations resource and the new `DELETE` read/clear it there.

**Permission catalog (`libs/mngr_latchkey/imbue/mngr_latchkey/`)**

- Add `minds-workspaces-update`, `minds-workspaces-recover`, `minds-workspaces-sharing` to `extensions/workspace_permissions.json` (all `targeted`/per-target), with dialog labels; they flow automatically through `permission_requests.mjs` (`computeWorkspaceEffect`) and `workspace_permissions.py` (dialog metadata). No startup sync. Cross-reference `apps/minds/docs/latchkey-permissions.md`.

**Electron (`apps/minds/electron/main.js`)**

- Repoint the create flow and the desktop quit-flow calls to the new routes. Verified via an Electron run (`just minds-start`); browser JS is not pytest-verifiable.

**Vocabulary cleanup**

- New routes and any user-facing strings refer to "workspace"/"desktop"/"provider" only; no agent/host/system-interface in URLs. HTML page routes (`/create`, `/creating/<id>`, `/destroying/<id>`, `/workspace/<id>/settings`, `/sharing/<id>/<svc>`, `/agents/<id>/recovery`) stay; only their data/JSON/SSE calls move.

**Migration discipline & docs**

- Remove each old `app.py` route as its flow is repointed and Electron-verified; no long-lived aliases.
- On implementation, add changelog entries for `apps/minds` and `libs/mngr_latchkey` (and `dev/` for this spec); update `apps/minds/docs/latchkey-permissions.md` with the three new verbs and the handoff's route table.
