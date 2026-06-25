# Minds workspace API — handoff (remaining work)

> **Primary spec / design doc (read this first):**
> [`blueprint/minds-workspace-api/plan-minds-workspace-api.md`](./plan-minds-workspace-api.md)
> Its "Refined prompt" section captures every locked design decision (the 37-question blueprint Q&A).

**Branch:** `mngr/minds-api-capabilities` · **PR:** [#2276](https://github.com/imbue-ai/mngr/pull/2276) (draft)
**FCT work:** external worktree at `.external_worktrees/forever-claude-template` (gitignored), branch `mngr/minds-api-capabilities`.

## What is already done (committed, tested)

- restic snapshot listing (`restic_cli.list_snapshots` + `ResticSnapshot`).
- `UNKNOWN`-credential grant-flow fix (`latchkey/handlers/predefined.py`): only `MISSING`/`INVALID` trigger credential setup. Prereq for all minds-internal scopes.
- **Telegram teardown — minds side only** (the `imbue/minds/telegram/` package, `/api/v1/.../telegram` + UI telegram routes, orchestrator, landing/settings UI, `TelegramError`s, `scripts/create_telegram_bot.py`).
- `original_minds_version` immutable label stamped at create.
- `minds-workspaces` detent permission scope + per-verb permissions + startup schema-sync (`libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py`, `store.list_host_permissions_paths`, `extensions/services.json`, `scripts/generate_services_json.py`).
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

## #1 — FCT telegram teardown (NOT started; minds side is done)

Remove all telegram from `forever-claude-template`. Work in `.external_worktrees/forever-claude-template` (branch already checked out, has the version-story commit).

Footprint (non-`vendor/`, non-`specs/`):
- Delete `libs/telegram_bot/`.
- Delete skills `.agents/skills/send-telegram-message/` and `.agents/skills/read-telegram-history/`.
- `.mngr/settings.toml` line ~8: drop `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_NAME` from `pass_env__extend` (the telegram *service* is already retired — see the comment near line 194).
- `CLAUDE.md` "Communication" section: `send-user-message` delegates to `send-telegram-message`; reword so the comms system no longer references telegram. **Verify `send-user-message` still works** (it probes channels and falls back to inline — confirm the fallback path).
- `README.md`, `pyproject.toml`: remove telegram mentions/deps.
- Check `apps/system_interface/imbue/system_interface/server.py` and `libs/cloudflare_tunnel/` + `libs/runtime_backup/README.md` for telegram references (grep `-rni telegram` excluding `vendor/`).
- Add an FCT changelog entry; run `cd apps/minds && uv run pytest` (and the FCT root suite) in the worktree.

**Risk:** telegram is woven into the comms path; a partial removal breaks `send-user-message`. Do it as one complete pass and verify the message-send path.

## #2 — UI browser-JS repoint onto `/api/v1` (dual-auth DONE; JS repoint NOT done)

Dual-auth already lets the UI call `/api/v1`. Remaining: change the browser `fetch` URLs to the v1 routes, reconciling response shapes. **Pytest cannot verify browser JS — this REQUIRES launching the Electron app** (`just minds-start`, see [`minds-dev-workflow`] / [`apps/minds/docs/desktop-app.md`](../../apps/minds/docs/desktop-app.md)).

Flows to repoint (old route → v1 route), with the static JS in `apps/minds/imbue/minds/desktop_client/static/`:
- Create: `POST /create` (303→`/creating`) + `creating.js` polling `/api/create-agent/<id>/status` + SSE `/api/create-agent/<id>/logs` → `POST /api/v1/workspaces` (`{operation_id}` 202) + `GET /api/v1/workspaces/operations/<id>` + `/logs`.
- Destroy: `/api/destroy-agent/<id>` + `/api/destroying/<id>/status` + `/log` (`destroying.js`) → `POST /api/v1/workspaces/<id>/destroy` + `operations/<id>` + `/logs`.
- Lifecycle: `/api/agents/<id>/start-host` / `/stop-host` (landing buttons) → `POST /api/v1/workspaces/<id>/start|stop`.
- Backups: `/api/backup-status` (batch) + `/api/backup-export/<id>` → per-workspace `GET /api/v1/workspaces/<id>/backups` + `POST .../backups/<snap>/export`.

**Decision to make:** response shapes differ (e.g. create returns `{agent_id,status}` vs `{operation_id,kind}`). Recommend keeping the v1 shapes canonical and updating the JS, not bending v1. The old `app.py` routes can stay as thin internal callers during transition, or be removed once the JS is fully repointed and Electron-verified.

## #3 — Per-target ("selected workspaces") permissions (verb-axis DONE; target-axis NOT done)

Today a `minds-workspaces-<verb>` grant applies to ALL workspaces. Add none/all/selected per target (spec Q3/Q11): listing stays all-or-nothing; per-target gates get-detail, version, backup-list/export, destroy, lifecycle, ssh.

Implementation:
- Generalize the existing per-agent allowlist machinery in `libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py` — `register_agent_for_host`, `_build_allowed_agent_anyof_entry`, `_extract_agent_id_from_anyof_entry`, and the `minds-api-proxy-per-agent-unauthorized` `not.anyOf` pattern — into a per-verb **target-id allowlist** for the `minds-workspaces` scope (the target workspace id appears in the request path, so the scope schema's path pattern carries the `anyOf` of allowed target ids).
- Carry a **target workspace id** in the permission request: model it on `LatchkeyFileSharingPermissionRequestEvent` (which carries a `path`) in `apps/minds/imbue/minds/desktop_client/request_events.py`; the grant writes the per-target allowlist entry.
- Dialog: `apps/minds/imbue/minds/desktop_client/latchkey/handlers/predefined.py` + `templates/pages/LatchkeyPredefinedPermission.jinja` + `templates.py` — present an all-vs-selected choice and name the target workspace.

## #4 — create backup + tunnel parity (account/key/region DONE)

Remaining: backup provisioning + Cloudflare tunnel injection for agent-created peers. These need the desktop create orchestration helpers, which live in `app.py` and can't be imported by `api_v1` (circular).

- Extract into a new shared module (e.g. `workspace_create.py`) imported by both: `_build_backup_request_or_error` (`app.py` ~913), `_build_on_created_callback` + `_OnCreatedCallbackFactory` (~879), `_resolve_effective_region` (~616), `_persist_region_for_launch_mode` (~636). Re-import them in `app.py` under the same names so its callsites (`_handle_create_agent_api` ~1159, `_handle_create_form_submit`) are unchanged.
- Then have `api_v1._handle_create_workspace` build `backup_request` + the `on_created` tunnel callback and pass them to `start_creation`.
- **High blast radius** (core create path). Verify with the full `desktop_client` suite AND the Electron create flow.

## #5 — SSH remote→local tunnel broker (remote-direct DONE)

`POST /api/v1/workspaces/<id>/ssh` returns 501 when the target is local (Docker/Lima; `get_ssh_info` is `None`). Build the hub-brokered tunnel for the remote-caller → local-target case (spec Q5/Q8):
- The calling (remote) workspace self-reports its workspace id (already accepted as `requester_workspace_id`). The hub runs one `ssh` process connecting the two machines (it can reach both) and returns a loopback port reverse-forwarded into the **caller's** container, so the caller connects to `127.0.0.1:<port>`.
- The tunnel process is owned by the Minds-app lifetime (dies with it) — own it on `get_state().root_concurrency_group`; likely a new small module + a registry of active tunnels in state.
- **Also wire `workspace_ssh.prune_expired_grant_lines`** (already written + unit-tested, but NOT wired): on each grant (and at `minds run` startup), read the target's `authorized_keys` via `mngr exec`, prune expired minds-owned lines, write back. See `apps/minds/imbue/minds/desktop_client/api_v1.py` `_handle_establish_ssh` and `workspace_ssh.py`.

Relevant: `api_v1.py` `_handle_establish_ssh`, `workspace_ssh.py`, `backend_resolver.get_ssh_info`, `mngr_forward.ssh_tunnel.RemoteSSHInfo`.

---

## Verification notes for whoever picks this up

- Tests: `just test-quick "apps/minds/imbue/minds/desktop_client"` and `just test-quick "libs/mngr_latchkey/..."`; full suite via `just test-offload`. Ratchets need staged changes (`git add` first).
- The agent-facing routes are exercised by `mngr exec`-style integration; the desktop UI flows (#2) need a real Electron run (`just minds-start`).
- Every touched project needs a `changelog/<branch>.md` entry (`apps/minds`, `libs/mngr_latchkey`, `dev/` for root files, and the FCT repo for #1).
