# Minds workspace API — handoff (remaining work)

> **Primary spec / design doc (read this first):**
> [`blueprint/minds-workspace-api/plan-minds-workspace-api.md`](./plan-minds-workspace-api.md)
> Its "Refined prompt" section captures every locked design decision (the 37-question blueprint Q&A).
> **Note:** the *permission model* in that plan was superseded by a simpler shipped
> design (see the superseded-notice at the top of the plan, and #3 below).

**Branch:** `mngr/more-minds-api` (continues `hynek/continue-minds-api`). The post-`main`-merge state is captured on the pushed base branch `josh/pre_final_minds_api` (in both this repo and FCT), so subsequent PRs diff against it to show only post-merge changes.
**FCT work:** external worktree at `.external_worktrees/forever-claude-template` (gitignored), branch `mngr/more-minds-api`; FCT telegram teardown is done (see #1).

> **Status (2026-06-26):** #1 (FCT telegram), #3 (per-target permissions), and #4
> (create backup/tunnel parity) are **done**. #2 (UI repoint) is **mostly done** --
> create/destroy/lifecycle/backups data calls run against `/api/v1`; the remaining
> old routes are listed in #2 and are kept deliberately (no v1 equivalent yet, or
> the create-flow poll not yet repointed). #5 (SSH remote→local broker + wiring
> `prune_expired_grant_lines`) is the main **outstanding** work.

## What is already done (committed, tested)

- restic snapshot listing (`restic_cli.list_snapshots` + `ResticSnapshot`).
- `UNKNOWN`-credential grant-flow fix (`latchkey/handlers/predefined.py`): only `MISSING`/`INVALID` trigger credential setup. Prereq for all minds-internal scopes.
- **Telegram teardown — minds side only** (the `imbue/minds/telegram/` package, `/api/v1/.../telegram` + UI telegram routes, orchestrator, landing/settings UI, `TelegramError`s, `scripts/create_telegram_bot.py`).
- `original_minds_version` immutable label stamped at create.
- `minds-workspaces` detent permission scope + per-verb permissions, **including the per-target ("selected") axis** (see #3). No startup schema-sync or baseline: the scope + verb schemas (and per-target schemas) are emitted *with each grant* and merged by name. Verb catalog in the shared `libs/mngr_latchkey/imbue/mngr_latchkey/extensions/workspace_permissions.json`; effect computed in `extensions/permission_requests.mjs` (`computeWorkspaceEffect`); Python dialog metadata in `mngr_latchkey/workspace_permissions.py`.
- `/api/v1/workspaces` **read** API: list, get, version (`workspace_version.py`), backups list, per-snapshot export (`backup_export.export_snapshot_zip`).
- `/api/v1/workspaces` **mutation**: create / destroy / lifecycle (start|stop) + operation-status polling + operation-logs SSE.
- SSH establish-access (`workspace_ssh.py`) — **remote targets only**.
- Dual-auth (`require_api_or_cookie_auth`) on all `/api/v1/workspaces` routes (cookie or bearer), so the UI *can* call them.
- Create-route params: `account_id`/`anthropic_api_key`/`region` with validation.
- FCT version story: `update-self` skill pins to the `minds-v*` tag series + structured merge messages; `parent.toml` annotated (committed in the FCT worktree, `1099fcde`).

Key code: `apps/minds/imbue/minds/desktop_client/api_v1.py` (the API), `workspace_version.py`, `workspace_ssh.py`, `backup_status.py`, `backup_export.py`, `backend_resolver.py` (`get_agent_label`), `agent_creator.py` (version label threading), `destroying.py` (`is_host_still_active`). Tests: `api_v1_test.py`, `workspace_version_test.py`, `workspace_ssh_test.py`, `agent_setup_test.py`.

Docs updated: [`apps/minds/docs/latchkey-permissions.md`](../../apps/minds/docs/latchkey-permissions.md). Related: [`apps/minds/docs/design.md`](../../apps/minds/docs/design.md), [`apps/minds/docs/overview.md`](../../apps/minds/docs/overview.md). Permission model upstreams: `~/project/latchkey`, `~/project/detent`.

## Cross-cutting constraints (read before touching the API)

- **Import direction:** `app.py` imports `api_v1.py` (`create_api_v1_blueprint`). So `api_v1` must NOT import `app.py`. Shared code goes in a *lower* module both import (e.g. a new `workspace_create.py`). This is why #2/#4 need extraction.
- **`origin/main` is merged into the branch** (the autofix agent did this mid-run). The PR diff against `main` (merge-base/three-dot) is clean; two-dot diffs falsely show main's commits.
- **Known unfixed NITPICK** (recorded in `.reviewer/outputs/autofix/unfixed/*.jsonl`): the `/api/v1/workspaces/<id>` routes call `AgentId(agent_id)` on the raw path param before the membership check, so a malformed id yields a logged 500 instead of 400/404 — matches the pre-existing `_handle_notification` convention; fix file-wide if desired.

---

## #1 — FCT telegram teardown — DONE

Telegram is removed from `forever-claude-template` (commit "Remove Telegram entirely from the template" on the FCT branch, the current tip captured by `josh/pre_final_minds_api`). `libs/telegram_bot/`, the `send-telegram-message`/`read-telegram-history` skills, the telegram service, and the `TELEGRAM_*` pass-env entries are gone, and `send-user-message` no longer routes to telegram.

The only remaining `telegram` mentions in the FCT tree are incidental and *not* part of the bot teardown: a few historical `specs/`/`blueprint/` docs use telegram as an example, and `.agents/skills/latchkey/SKILL.md` lists "Telegram" as one of many third-party services latchkey *itself* can broker (unrelated to the removed minds bot). No action needed.

## #2 — UI browser-JS repoint onto `/api/v1` (mostly DONE)

Most UI data calls already run against `/api/v1`. What's been repointed (verified by grepping `static/`, `templates/pages/*.jinja`, and `electron/`):

- **Destroy** (`destroying.js`, `WorkspaceSettings.jinja`): `POST /api/v1/workspaces/<id>/destroy`, status via `GET /api/v1/workspaces/operations/<id>`, logs via `.../operations/<id>/logs`.
- **Lifecycle** (`Landing.jinja`, `electron/main.js` quit flow): `POST /api/v1/workspaces/<id>/start|stop`.
- **Backups** (`Landing.jinja`): per-workspace `GET /api/v1/workspaces/<id>/backups` + per-snapshot `POST .../backups/<snap>/export`.

### Old routes still in use (intentional divergences)

These are **deliberately still on the old `app.py` routes**, either because there is no v1 equivalent (the v1 surface is scoped to cross-workspace *workspace* management, not app-/UI-local concerns) or because the flow hasn't been repointed yet. **Pytest cannot verify browser JS — repointing any of these REQUIRES an Electron run** (`just minds-start`).

**A. No v1 equivalent (keep as-is unless the v1 surface is deliberately broadened):**

| Old route | Method | Caller | Note |
|---|---|---|---|
| `/api/backup-status` | GET | `Landing.jinja` | *Batch* status for all workspaces at once (badges); v1 only has per-workspace `/workspaces/<id>/backups`. |
| `/api/destroying/<id>/dismiss` | POST | `destroying.js` | UI-only "dismiss the destroyed card" action. |
| `/api/workspaces/<id>/color` | POST | `workspace_settings.js` | Workspace color is UI metadata, not in the cross-workspace API. |
| `/api/providers/<name>/toggle` | POST | `Landing.jinja` | App-level provider enable/disable. |
| `/api/agents/<id>/host-health`, `/restart-system-interface`, `/restart-host`, `/agents/<id>/recovery` | GET/POST | `electron/main.js`, recovery page | System-interface recovery flow. |
| `/api/minds/running`, `/api/minds/stop-hosts`, `/api/minds/stop-state-container` | GET/POST | `electron/main.js` (quit prompt) | App-level minds lifecycle (bulk stop / quit). |
| `/api/sharing-status/<id>/<svc>`, `/api/sharing-readiness/<id>/<svc>`, `/sharing/<id>/<svc>/enable\|disable` | GET/POST | `sharing.js` | Cloudflare forwarding flow; not part of the workspaces API. |

**B. Has a v1 equivalent but the UI flow is NOT yet repointed (the one real remaining migration):**

- **Create:** the browser create flow still posts the HTML form to `/create` (→ 303 `/creating/<id>`) and `creating.js` then polls `/api/create-agent/<id>/status` + SSE `/api/create-agent/<id>/logs`. The v1 equivalents exist (`POST /api/v1/workspaces` + `GET /api/v1/workspaces/operations/<id>` + `/logs`) and the *agent* path uses them, but the browser create UI wasn't moved over. Response shapes differ (old `{status, redirect_url}` poll vs v1 operation resource); repointing means updating `creating.js` (and likely keeping the `/create` HTML page submit, just changing the status/logs polling). Electron-verify after.

Note: HTML *page* routes (`/create`, `/creating/<id>`, `/destroying/<id>`, `/_chrome/*`, `/inbox*`, `/sharing/*`, `/accounts`, `/settings`, `/workspace/<id>/settings`, recovery page) are intended to stay (the plan always kept one HTML surface); they are not "divergences".

## #3 — Per-target ("selected workspaces") permissions — DONE (via a simpler design than originally planned)

Both axes are implemented. Listing/create are all-or-nothing; `destroy`, `lifecycle`, `backups-export`, and `ssh` are per-target (none/all/selected). **This shipped with a different, simpler mechanism than this handoff originally proposed** — it does *not* generalize the per-agent `anyOf` allowlist. Canonical reference: [`apps/minds/docs/latchkey-permissions.md`](../../apps/minds/docs/latchkey-permissions.md) ("Cross-workspace management API permissions").

How it actually works:
- A dedicated `type=workspace` permission request (distinct from `predefined` and `file-sharing`) carries the verbs and, for target-scoped verbs, the `target_workspace_id`. See `apps/minds/imbue/minds/desktop_client/request_events.py` and the gateway client.
- The grant carries a precomputed `effect` (scope schema + verb schemas + rule) built by `computeWorkspaceEffect` in `libs/mngr_latchkey/imbue/mngr_latchkey/extensions/permission_requests.mjs`, applied via the standard `POST /permission-requests/approve/<id>` path (the approve call sends an override body so the gateway recomputes from the user's dialog choices).
- A **selected** grant mints a uniquely-named per-target schema `minds-workspaces-<verb>-<target_id>`; successive selected grants *accumulate* targets through the gateway's ordinary **schema-by-name merge** (the same mechanism file-sharing uses for per-path schemas) — **no `anyOf`, no per-host allowlist construct, no startup migration**. An "all workspaces" grant uses the broad verb schema with a `[^/]+` id wildcard.
- Verb catalog (scope/schema names, targeted split, dialog labels) is a single shared file `libs/mngr_latchkey/imbue/mngr_latchkey/extensions/workspace_permissions.json`, read by both `mngr_latchkey/workspace_permissions.py` (dialog metadata) and `permission_requests.mjs` (schema construction); `permission_requests_test.py` asserts they don't drift.
- Dialog: the inbox handler/templates present a per-verb checkbox set plus an all-vs-selected choice naming the target workspace.

## #4 — create backup + tunnel parity — DONE

The create helpers were extracted into `apps/minds/imbue/minds/desktop_client/workspace_create.py` (`build_backup_request_or_error`, `build_create_on_created_callback` + the `CreateOnCreatedCallback` class, `resolve_effective_region`, `default_region_for_provider_with_config`), imported by both `app.py` and `api_v1.py` (this is also why the `main` merge had to reconcile `app.py` — the in-`app.py` copies were removed). `api_v1._handle_create_workspace` now builds `backup_request` via `build_backup_request_or_error` and the `on_created` callback via `build_create_on_created_callback` (which injects the Cloudflare tunnel token + associates the account) and passes both to `start_creation`, matching the desktop UI's create path.

Note: legacy `/api/create-agent` (JSON) and the onboarding-questions feature were removed during the `main` merge; the browser create flow still uses the older `/api/create-agent/<id>/status|logs` *polling* routes (see #2.B).

## #5 — SSH remote→local tunnel broker — OUTSTANDING (the main remaining work; remote-direct DONE)

`POST /api/v1/workspaces/<id>/ssh` works for remote targets but returns 501 when the target is local (Docker/Lima; `get_ssh_info` is `None` — see `api_v1.py` `_handle_establish_ssh`, the explicit 501 with "brokering a forwarding tunnel for the remote->local case is not yet supported"). Build the hub-brokered tunnel for the remote-caller → local-target case (spec Q5/Q8):
- The calling (remote) workspace self-reports its workspace id (already accepted as `requester_workspace_id`). The hub runs one `ssh` process connecting the two machines (it can reach both) and returns a loopback port reverse-forwarded into the **caller's** container, so the caller connects to `127.0.0.1:<port>`.
- The tunnel process is owned by the Minds-app lifetime (dies with it) — own it on `get_state().root_concurrency_group`; likely a new small module + a registry of active tunnels in state.
- **Also wire `workspace_ssh.prune_expired_grant_lines`** (already written + unit-tested, but NOT wired): on each grant (and at `minds run` startup), read the target's `authorized_keys` via `mngr exec`, prune expired minds-owned lines, write back. See `apps/minds/imbue/minds/desktop_client/api_v1.py` `_handle_establish_ssh` and `workspace_ssh.py`.

Relevant: `api_v1.py` `_handle_establish_ssh`, `workspace_ssh.py`, `backend_resolver.get_ssh_info`, `mngr_forward.ssh_tunnel.RemoteSSHInfo`.

---

## Verification notes for whoever picks this up

- Tests: `just test-quick "apps/minds/imbue/minds/desktop_client"` and `just test-quick "libs/mngr_latchkey/..."`; full suite via `just test-offload`. Ratchets need staged changes (`git add` first).
- The agent-facing routes are exercised by `mngr exec`-style integration; the desktop UI flows (#2) need a real Electron run (`just minds-start`).
- Every touched project needs a `changelog/<branch>.md` entry (`apps/minds`, `libs/mngr_latchkey`, `dev/` for root files, and the FCT repo for #1).
