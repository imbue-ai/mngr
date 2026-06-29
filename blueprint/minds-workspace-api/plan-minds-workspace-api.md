# Plan: Minds workspace API (cross-workspace capabilities via latchkey)

> **Superseded — permission model.** The permission design described below
> (a `minds` detent scope whose schemas are defined once in code and
> *idempotently synced into every per-host file on `minds run` startup*, with
> the per-target axis built by *generalizing the per-agent `anyOf` allowlist*)
> was **not** how this shipped. The implemented model is simpler and is the
> canonical reference: see
> [`apps/minds/docs/latchkey-permissions.md`](../../apps/minds/docs/latchkey-permissions.md)
> ("Cross-workspace management API permissions"). In short:
>
> * The scope is named **`minds-workspaces`** (not `minds`), with one named
>   permission per verb (`minds-workspaces-read`, `-create`, `-destroy`,
>   `-lifecycle`, `-backups-export`, `-ssh`).
> * **No startup schema-sync and no baseline migration.** The scope + verb
>   schemas are emitted, fully self-described, *with each grant* and merged
>   into the requesting agent's per-host file by name -- a host that has never
>   seen the scope gets it on the first grant.
> * The per-target ("selected") axis uses a **uniquely-named per-target verb
>   schema** (`minds-workspaces-<verb>-<target_id>`) that pins one workspace;
>   successive selected grants *accumulate* via the gateway's ordinary
>   schema-by-name merge -- **no `anyOf`, no per-host allowlist generalization,
>   no special merge logic** (the same mechanism file-sharing uses for
>   per-path schemas).
> * The grant is applied like file-sharing: the agent posts a `type=workspace`
>   permission request, the effect (scope schema + verb schemas + rule) is
>   computed in the gateway extension's `computeWorkspaceEffect`
>   (`permission_requests.mjs`), and the desktop client approves it via
>   `POST /permission-requests/approve/<id>`. The Python
>   `mngr_latchkey.workspace_permissions` module holds only the dialog-facing
>   verb metadata.
>
> The rest of this plan (API surface, operations, version, backups, SSH,
> telegram removal) describes what shipped; only the permission *plumbing*
> bullets in the "Refined prompt", "Overview", and "Changes" sections below
> reflect the original (abandoned) design and should be read through this note.

## Refined prompt

> **Expose the basic Minds functionality through a versioned API (via latchkey) so that Minds workspaces can themselves interact with other workspaces, backups, etc.** — CRUD/listing for workspaces, workspace version data, enabling SSH access into workspaces, and read/listing for backups, with permission scoping that allows none/all/selected workspaces.
>
> **API surface & hub model**
> * Versioned `/api/v1/workspaces/...` in `api_v1.py`, reached by agents via the latchkey gateway's `minds-api-proxy` (hub model — requires the Minds app online)
> * The `minds-api-proxy` extension already forwards any `/minds-api-proxy/...` path with gating left to detent, so no latchkey/proxy change is needed — only minds-side routes + detent scope/permissions
> * UI's JSON/data calls hit `/api/v1/...` directly; HTML page routes stay (one HTTP surface, one implementation)
> * New routes accept session cookie OR central bearer (reuse `_is_api_authenticated`)
> * Remove the unused `/api/v1` **telegram** route; keep **notifications** + per-agent self-scoping
> * Workspace identified by its primary (`is_primary`+`workspace`) agent id; names display-only
>
> **Operations & lifecycle**
> * Long-running create/destroy = operation resource at `/api/v1/workspaces/operations/<op_id>` (+`/logs`), polled by unguessable id; operation-read bundled into the verb grant
> * start/stop (lifecycle) routes block until the transition resolves and return the final state
> * Create exposes the full form parameter surface to agents; finer restriction deferred to future latchkey one-off requests; no abuse guard in scope (noted gap)
> * Agent-created peers skip onboarding (default `CONTROL`, gather nothing) and start with the standard deny-all baseline (creator gets no automatic access)
> * Destroy tears down host/agent but leaves backups + `restic.env` intact
>
> **Permissions**
> * One `minds` detent scope, per-verb permissions (read / create / destroy / backups / ssh); `minds` schemas defined once in code and idempotently synced into every per-host file on `minds run` startup
> * Port the `UNKNOWN`-credential-status grant-flow fix (only `MISSING`/`INVALID` trigger credential setup)
> * Listing all workspaces is all-or-nothing by verb; per-target (selected) axis gates get-detail, version, backup-list/export, destroy, lifecycle, ssh — each verb's target allowlist is independent
> * A permission request names one target + the verbs; user approves that bundle; re-requesting another target adds to that verb's allowlist
>
> **Version data**
> * Immutable `original_minds_version` label at create = the resolved template ref verbatim (`minds-v*` tag in prod; branch/`"main"` in dev)
> * `parent.toml` pinned to the `minds-v*` tag series; upgrade history = successful-merge commits in the workspace's git only
> * Version route returns `{original, current (best-effort `git describe --match minds-v*`), upgrade_merges[{sha,date,to_version}]}` read from the system-services agent's work_dir on its primary branch via lazy/on-demand hub `mngr exec`; offline → original only; `to_version` is best-effort nearest tag
>
> **Backups**
> * Stream snapshot zip through the gateway proxy; add per-snapshot selection via new `restic snapshots --json` listing
> * `GET /api/v1/workspaces` includes destroyed-but-backed-up workspaces inline (marked with state); for destroyed targets only backup-list/export + knowable get-detail/version work, mutate/ssh rejected
>
> **SSH**
> * Caller generates its keypair, sends only the public key; hub injects it into the target's `authorized_keys` (tagged with requester + 24h TTL, auto-pruned at TTL and on app startup) and brokers an ephemeral tunnel bounded by the key TTL and the Minds-app lifetime
> * Single "establish access" route: takes the public key + the caller's own (untrusted, self-reported) workspace id, injects the key, opens the tunnel, returns host:port + TTL; re-requests refresh/reuse; requires target in the caller's ssh allowlist; errors if target stopped
>
> **Scope & phasing**
> * Everything above, in phases: P1 UI refactor onto the versioned API → P2 permission plumbing + read routes → P3 version → P4 backups → P5 create/destroy/lifecycle → P6 SSH
> * FCT changes (parent.toml pin, update-self records merge messages) in scope via an external worktree
> * Remove all telegram, minds + FCT
> * Changelog entries for `apps/minds` + `libs/mngr_latchkey` (and FCT's own); update `apps/minds/docs/latchkey-permissions.md`

---

## Overview

- Promote the minds desktop client's ad-hoc, UI-only capabilities (create / destroy / lifecycle / backups) into a **single versioned `/api/v1/workspaces/...` API** that both the browser UI and in-workspace agents call — one implementation behind one HTTP surface, reached by agents through the existing latchkey `minds-api-proxy` (the proxy already forwards any path, so gating is pure detent — no latchkey change).
- Add a **`minds-workspaces` detent scope with per-verb permissions** (read / create / destroy / lifecycle / backups-export / ssh), each grantable for **none / all / selected** target workspaces, surfaced through the existing permission-request → inbox → grant dialog. The "selected" axis mints a uniquely-named per-target verb schema and accumulates targets through the gateway's ordinary schema-by-name merge (the same mechanism file-sharing uses for per-path schemas) -- not the per-host `anyOf` allowlist this plan originally proposed; see the superseded-notice at the top.
- Add genuinely-new capabilities agents need to "adapt work from another workspace": **workspace version data** (immutable `original_minds_version` label + git-derived upgrade history), **backup listing + per-snapshot export** (works even for offline/destroyed workspaces because the hub holds each `restic.env`), and **hub-brokered ephemeral SSH access** (caller ships only a public key; the hub injects a TTL-tagged key and brokers a tunnel that dies with the app).
- Treat the prior `yash/spawn-peer-minds` branch as the proven seed: reuse its dual cookie-or-bearer auth, its inline-schema-in-code + startup-sync approach, and its `UNKNOWN`-credential-status grant-flow fix (every minds-internal scope reports `UNKNOWN`, so without the fix grants would wrongly demand credentials).
- **Remove telegram entirely** (minds desktop subsystem + FCT skills/bot/service) — it was a redundant channel for minds workspaces (web UI is inbound, the kept `notifications` API is outbound), and removing it shrinks the surface this work has to carry.
- Deliver in phases that each leave a working system, starting with a **no-new-capability UI refactor** onto the new API so the contract is proven against the existing UI before any agent can call it.

## Expected behavior

**From an in-workspace agent (through the latchkey gateway):**

- An agent that has never been granted `minds` access gets a 403 on its first call; it files a permission request (`scope=minds`, the verbs it wants, a target workspace where applicable, a rationale) and waits. The user sees an inbox card, picks verbs and **all-vs-selected** target, and approves; the agent is messaged and retries successfully. Subsequent same-scope calls need no dialog.
- `GET /api/v1/workspaces` (needs the `list` grant) returns all workspaces, **including destroyed-but-still-backed-up ones**, each marked with state. Listing is all-or-nothing; it does not leak per-target data.
- For a specific workspace **B**, an agent can call get-detail, version, backup-list/export, destroy, lifecycle, or ssh **only if B is in that verb's allowlist**. Each verb's allowlist is independent (read-B without ssh-B is expressible).
- **Create** returns immediately with an operation handle; the agent polls `GET /api/v1/workspaces/operations/<op_id>` and may stream `/logs` (both covered by the create grant, by unguessable id) until the operation completes and exposes the new workspace's id. The new peer starts with a deny-all baseline (the creator gets no automatic access to it) and skips onboarding.
- **Destroy** also returns an operation handle and is followed the same way. After destroy, the workspace still appears in the list (marked destroyed) and its backups remain listable/exportable.
- **start/stop** block until the host transition resolves and return the final state.
- **Version**: `GET .../version` returns `original_minds_version` (always, from the create-time label), plus — when the workspace is online — a best-effort `current` (via `git describe --match 'minds-v*'`) and an `upgrade_merges` list (sha, date, best-effort nearest `to_version`) read from the workspace's git via the hub. Offline/destroyed → only `original`.
- **Backups**: `GET .../backups` lists restic snapshots (id, time, size); `POST .../backups/<snapshot>/export` streams a zip of that snapshot through the gateway. Both work even if the workspace is offline/destroyed (hub holds the `restic.env`).
- **SSH**: the caller generates its own keypair and calls the single "establish access" route with its public key **and its own (self-reported) workspace id**. The hub injects the TTL-tagged public key into B's `authorized_keys`, brokers an ephemeral tunnel reverse-forwarded into the caller's container, and returns a loopback host:port + TTL. The caller then runs ordinary `ssh`/`git`/`rsync`. Re-requests refresh rather than stack. If B is stopped, the route errors and tells the caller to start it first.

**From the browser UI:**

- No user-visible behavior change from the refactor: the UI's data calls now hit `/api/v1/workspaces/...` (cookie-authenticated), backed by the same functions as before. HTML page routes are unchanged.
- The permission inbox dialog gains the `minds` scope with its verb checkboxes and an all-vs-selected target choice (built on the existing dialog, with the `UNKNOWN`-status fix so it never wrongly prompts for credentials).

**Removed behavior:**

- All telegram functionality disappears: no minds telegram setup UI/API/orchestrator/injection, and FCT no longer ships telegram skills, the bot library, or the telegram background service. Workspaces communicate via the web UI (inbound) and the `notifications` API (outbound); `notifications` is otherwise unchanged.

**Cross-cutting:**

- Everything requires the Minds desktop app to be online (hub model). Backups/version-original work for offline/destroyed targets; live version, lifecycle, and SSH require the target online.
- The two end-to-end skill scenarios become possible: "source online" (create → ssh → pull/rsync → adapt, using live version data) and "backups only" (list backups → export snapshot → adapt), the latter working even after the source is destroyed.

## Changes

**New versioned API (`apps/minds`, in/around `desktop_client/api_v1.py`)**

- Add a `/api/v1/workspaces` resource: list, get-detail, version, backups (list + per-snapshot export), create, destroy, lifecycle (start/stop), and establish-ssh-access routes.
- Add an operation resource (`/api/v1/workspaces/operations/<op_id>` + `/logs`) generalizing today's creation/destroy status+log streaming, keyed by unguessable id, with operation-read bundled into the spawning verb's grant.
- Authenticate all new routes via session cookie OR central bearer (reuse the dual-auth helper from the prior branch).
- Keep `/api/v1/agents/<id>/notifications` and the per-agent self-scope unchanged.

**Refactor UI onto the API (`apps/minds`)**

- Point the browser UI's data/JSON calls at the new `/api/v1/workspaces/...` routes; retire the ad-hoc `/api/create-agent`, `/api/destroy-agent`, `/api/backup-*`, `/api/agents/<id>/*-host`, etc. JSON endpoints in favor of the versioned ones (HTML page routes stay).
- Ensure both front doors call the same underlying service functions (agent creator, destroying, backup status/export, backend resolver) so there is one implementation.

**Permissions (`libs/mngr_latchkey` + `apps/minds`)** *(shipped design; see the superseded-notice at the top of this doc and [`latchkey-permissions.md`](../../apps/minds/docs/latchkey-permissions.md))*

- Define a `minds-workspaces` detent scope with one named permission per verb (`minds-workspaces-read`, `-create`, `-destroy`, `-lifecycle`, `-backups-export`, `-ssh`). The verb catalog (scope name, schema names, targeted/non-targeted split, dialog labels) lives in a single shared `extensions/workspace_permissions.json` read by both the Python dialog metadata (`mngr_latchkey.workspace_permissions`) and the gateway extension.
- **No startup schema-sync and no baseline.** The scope + verb schemas are emitted with each grant (self-described `effect`) and merged into the requesting agent's per-host file by name, so a host that has never seen the scope gets it on the first grant.
- Per-target ("selected") grants for the target-scoped verbs (`destroy`, `lifecycle`, `backups-export`, `ssh`) mint a uniquely-named per-target schema (`minds-workspaces-<verb>-<target_id>`); successive selected grants accumulate via the ordinary schema-by-name merge (no `anyOf`, no per-host allowlist construct). `read` and `create` are all-or-nothing.
- Extend the permission-request model + inbox dialog to carry a target workspace and verb selection, and to present the none/all/selected choice. This is a dedicated `type=workspace` request type (distinct from `predefined` and `file-sharing`); the effect is computed in the gateway extension's `computeWorkspaceEffect` and applied via the standard `POST /permission-requests/approve/<id>` path (the approve call sends an override body so the gateway recomputes from the user's dialog choices).
- Port the `UNKNOWN`-credential-status grant-flow fix so minds-internal scopes are granted without a spurious credential step.

**Version data (`apps/minds` + FCT)**

- Stamp an immutable `original_minds_version` label at workspace create time from the resolved template ref.
- Add a version-read path that returns the label plus, when online, git-derived current version + upgrade-merge history via the hub `mngr exec`ing into the system-services agent's work_dir.
- FCT: pin `parent.toml` to the `minds-v*` tag series, and have `update-self` fetch tags and record structured "from → to" merge-commit messages so the git log is self-describing.

**Backups (`apps/minds`)**

- Add a restic snapshot **listing** capability (`restic snapshots --json`) and a typed snapshot model.
- Generalize the existing latest-snapshot export to export an arbitrary snapshot id, and expose listing + export through the API (streamed through the gateway), addressable by workspace id including destroyed ones.

**SSH (`apps/minds`)**

- Add an authorized-key injection path that appends a caller-supplied public key (tagged with requester + 24h TTL) to a target workspace's `authorized_keys`, with auto-pruning at TTL and on app startup.
- Add a hub-brokered ephemeral SSH-forwarding tunnel owned by the app's lifetime (dies with the app), reverse-forwarded into the caller's container, returning a loopback host:port + TTL; refresh-on-re-request; per-target gated; errors if the target is stopped.

**Telegram removal (`apps/minds` + FCT)**

- minds: remove the `/api/v1` telegram route, the UI telegram routes, `TelegramSetupOrchestrator`, the whole `imbue/minds/telegram/` package, create-time telegram injection, and their tests.
- FCT: remove telegram skills (`send-telegram-message`, `read-telegram-history`), `libs/telegram_bot`, the telegram background service (services.toml/supervisord), and the `TELEGRAM_BOT_TOKEN`/`TELEGRAM_USER_NAME` pass-env + README references.

**Docs, changelog, and mechanics**

- Update `apps/minds/docs/latchkey-permissions.md` for the `minds` scope/verbs/per-target model and remove telegram references; add a design/spec doc for the workspace API.
- Add changelog entries for `apps/minds` and `libs/mngr_latchkey` (and FCT's own changelog); do FCT changes in an external worktree under `.external_worktrees/`.

**Phasing**

- P1: refactor the UI onto the new `/api/v1/workspaces` surface (no new capability) + telegram removal.
- P2: permission plumbing (`minds` scope, per-target allowlist, dialog, `UNKNOWN` fix, startup sync) + agent-facing read routes (list, get-detail).
- P3: version data (label at create, FCT parent.toml/update-self, version route).
- P4: backups (snapshot listing + per-snapshot export route).
- P5: create / destroy / lifecycle routes for agents (operation resource).
- P6: SSH (key injection + brokered tunnel).

## Notes / known gaps

- No server-side abuse guard on agent-driven create in this scope (rely on the user grant; finer control deferred to future latchkey one-off requests).
- Live version data, lifecycle, and SSH require the target workspace online; only `original_minds_version` and backups are available for offline/destroyed targets.
- `current`/`to_version` are best-effort nearest `minds-v*` tags (a workspace can sit between tags); failed/aborted upgrade attempts are not recorded (only successful merges live in git).
- SSH-establish trusts the caller's self-reported workspace id for the reverse-forward destination (untrusted; worst case the tunnel lands in the wrong place).
