# Latchkey permissions

Minds-managed agents access third-party services (Slack, GitHub, Google Drive,
...) through [Latchkey](https://github.com/imbue-ai/latchkey). This page
describes how the desktop client surfaces permission decisions to the user
and how the agent receives the answer.

## End-to-end flow

1. **Agent makes a call.** The agent issues an HTTP request to the
   minds-managed shared `latchkey gateway` (or to `latchkey curl`
   directly). The agent's environment carries the gateway URL, a shared
   password (sent in `X-Latchkey-Gateway-Password`) and a permissions
   override JWT (sent in `X-Latchkey-Gateway-Permissions-Override`) that
   points the gateway at the agent's own permissions file.
2. **Gateway responds with success, no-credentials, or not-permitted.**
   * 200: success, nothing to do.
   * 400 with `Error: No credentials found for <service>` (or `... are expired`):
     the user has not yet authenticated to the service.
   * 403 with `Error: Request not permitted by the user.`: the user has
     authenticated but has not allowed this kind of request.
3. **Agent writes a request event.** On any of the blocked outcomes, the
   agent appends a `LatchkeyPredefinedPermissionRequestEvent` to
   `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl` with the latchkey
   service name and a one-paragraph rationale, then ends its turn and goes
   idle.
4. **Desktop notifies the user.** The desktop client tails the agent's
   request events file via `mngr event --follow`, adds a card to the
   inbox drawer, and surfaces a notification.
5. **User opens the dialog.** Clicking the card opens
   `/inbox?selected=<event_id>` in a **modal overlay** over the current
   window (a transparent full-window `WebContentsView` stacked above the
   workspace, with a dim backdrop). The user's workspace view is never
   navigated away, so dismissing the dialog -- via Approve/Deny, the close
   button, a backdrop click, or Escape -- returns them to their work with
   no context lost. (Opened directly in a browser, with no modal host, the
   page degrades to a dimmed, centered card and dismissal navigates home.)
   The page renders a single-scope permission dialog:
   * The dialog header names the service plainly (no monospace pill) and
     attributes the agent's rationale prominently as
     "`<workspace>` says:" -- this is the main place the requesting
     agent's name is surfaced. There is no separate "Workspace:" line.
   * By default the dialog shows a **simple, informative view**: a
     single summary sentence ("Approving will grant `<workspace>` and its
     sibling agents the following permissions:") above a read-only list
     of the permissions that will be granted on Approve (no checkboxes),
     plus only the Approve / Deny buttons. This keeps the common case
     approachable for non-technical users.
   * A small **"Adjust"** link, rendered inside the permission list, reveals
     the full **editor view**, which exposes a checkbox per [Detent](https://github.com/imbue-ai/detent)
     permission schema available for that scope. The available schemas
     are read from the bundled `services.json` catalog (shipped with
     mngr_latchkey) and cached in process for the lifetime of the desktop
     client. The checkbox inputs always exist in the page (the editor is
     merely hidden by default), so the simple view's Approve still
     submits the pre-checked set.
   * The detent ``any`` schema (matches every request inside the scope) is
     prepended as the first checkbox in the editor so the user can opt
     into unrestricted access if they want. It is **not** pre-checked,
     and so never appears in the simple view's read-only list.
   * The dialog pre-checks (and the simple view lists) the union of (a)
     permissions already granted for that scope on the agent's host and
     (b) the permissions the agent declared in the request event.
     Approving without changes grants exactly that union; opening the
     editor and ticking more broadens it, unticking narrows or revokes.
     The editor therefore doubles as a revocation UI.
   * The Approve button stays disabled while zero boxes are checked,
     so if the agent submitted an empty ``permissions`` tuple and the
     user has no prior grants for the scope, the simple view shows a
     prompt to use "Adjust" and the user must actively pick something
     there before approving.
6. **User approves.** The desktop client:
   1. Runs `latchkey services info <service>` to read `credentialStatus`,
      `authOptions`, and `setCredentialsExample`. A `valid` *or*
      `unknown` status skips credential setup entirely and proceeds
      straight to the grant (step 6.4) -- `unknown` means latchkey
      cannot vouch for the credential either way (e.g. a generic
      `rawCurl` credential it has no validator for, or a catalog scope
      like `minds` that is not a registered latchkey service at all),
      so prompting the user would demand credentials that already exist
      or were never theirs to manage.
   2. If credentials are reported as `missing` or `invalid` and the
      service advertises a `browser` auth option (or latchkey reports no
      `authOptions` at all, treated as the legacy fallback), runs
      `latchkey auth browser <service>` synchronously (transparently
      running the one-off `latchkey auth browser-prepare <service>`
      step first when latchkey asks for it).
      Cancellation or failure of either step produces a `FAILED` outcome:
      the grant is **not** applied and the request stays pending (no
      response event is written), so the dialog surfaces the reason and the
      user can click Approve again to retry. A failed approval is never
      recorded as a denial.
   3. If credentials are reported as `missing` or `invalid` and the
      service does not advertise a `browser` auth option (e.g. Coolify,
      where `authOptions = ["set"]`),
      the grant is **refused** and the request stays pending. The dialog
      shows the `setCredentialsExample` returned by latchkey (or a
      generic fallback) and asks the user to run it in a terminal. A
      subsequent Approve click re-runs `latchkey services info` and
      proceeds normally once credentials are valid.
   4. Atomically rewrites the agent's `latchkey_permissions.json` so the gateway
      enforces the chosen schemas on the next request.
   5. On success, appends a `GRANTED` response event to
      `~/.minds/events/requests/events.jsonl`. (A `FAILED` approval writes
      no response event and leaves the request pending; see step 6.2.)
   6. On a `GRANTED` outcome, sends the agent a plain-English `mngr message`
      describing the decision; the agent wakes up and decides whether to
      retry. A `FAILED` or manual-credentials outcome leaves the request
      pending and notifies only the user (in the dialog), not the agent.
7. **User denies.** The desktop client appends a `DENIED` response event
   and sends the agent a plain-English denial message. `latchkey_permissions.json`
   is not touched.

## Per-agent isolation

Minds runs a single shared `latchkey gateway` subprocess for every
agent rather than one per agent. The gateway is locked down with two
latchkey 2.8.0 features:

* **Password protection.** The gateway is started with
  `LATCHKEY_GATEWAY_LISTEN_PASSWORD` set, so it rejects every request
  that does not present the same value in the
  `X-Latchkey-Gateway-Password` header. The password is derived
  deterministically from the desktop client's Latchkey encryption key:
  minds calls `latchkey gateway create-jwt --no-validate` against a
  hard-coded sentinel path and SHA-256-hashes the resulting JWT. That
  way the password is stable across desktop-client restarts without
  minds having to persist it in plaintext anywhere.
* **Per-agent permission overrides.** When an agent is created, minds
  allocates an opaque
  `~/.minds/latchkey/permissions/<uuid>.json` handle, materializes it
  with empty `rules` (deny-all baseline), and mints a
  permissions-override JWT pointing at that path via
  `latchkey gateway create-jwt`. The JWT is injected into the agent's
  environment as `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` *at*
  `mngr create` *time*, so the agent's first ever `latchkey` call
  already carries it in the
  `X-Latchkey-Gateway-Permissions-Override` header.

  After `mngr create` returns the canonical agent id, minds replaces
  the opaque file with a symlink pointing at
  `~/.minds/agents/<agent_id>/latchkey_permissions.json`. The agent-id
  path is the canonical location -- the desktop client's permission-grant
  flow writes to it as before -- and the gateway reads through the
  symlink to see those grants. This indirection lets minds mint and
  inject the JWT before the agent id is known, eliminating a
  previously-fragile post-create injection step.

## Minds API access through the gateway

Minds itself exposes a small REST API on the desktop-client bare
origin (`/api/v1/...`: agent notifications, Telegram bot setup, the
WebDAV file-sharing mount). Agents reach it through the same latchkey
gateway they use for every other outbound HTTP call, via the bundled
`minds-api-proxy` extension at `/minds-api-proxy/api/v1/...`. There is
no per-agent reverse SSH tunnel for the Minds API anymore.

Authentication uses one central `MINDS_API_KEY` per `minds run`,
freshly generated in memory at startup and never handed to agents.
The `minds-api-proxy` extension reads it from the
`LATCHKEY_EXTENSION_MINDS_API_KEY` env var (published to the supervisor
by `minds run`, which restarts the supervisor on every startup so the
current key always wins) and injects `Authorization: Bearer <key>` on
every forwarded request, overwriting any header the agent supplied.
The desktop client matches the same value on the inbound side. The
key rotates per minds startup; nothing else in the monorepo reads it
from disk, so there is no on-disk copy to keep in sync.

Per-agent isolation comes from the latchkey gateway's permissions
file. The agent baseline grants every agent one shared call --
`POST /minds-api-proxy/api/v1/agents/<...>/notifications` -- so any
workspace the desktop client created can always notify the user. For
the other routes (Telegram setup, future `/api/v1/agents/<id>/*` endpoints,
the WebDAV mount), agent creation installs a *per-agent* rule + inline
schemas in the host's permissions file: the scope schema
`minds-api-self-<agent_id>` mirrors `latchkey-self.invalid` and the
permission schema `minds-api-proxy-call-<agent_id>` pins the URL
path to `/minds-api-proxy/api/v1/agents/<agent_id>/...`. Because the
file is keyed per host, an agent on host A cannot reach the API on
behalf of an agent on host B: host A's permissions file does not list
B's agent id at all.

The gateway's *default* permissions config
(`~/.minds/latchkey_default_permissions.json`) is materialized with
empty `rules` too, so any request that somehow bypasses the JWT
mechanism still sees a deny-all gateway -- the implicit `allow all`
that latchkey applies when the file is missing must never be observable
by an agent.

`LATCHKEY_DIRECTORY` -- where credentials live -- stays shared across all
agents on the same machine.

## Service catalog

The catalog of latchkey services (display name + scope schema + the
permission schemas the dialog offers) lives alongside the latchkey
gateway extension at
[`libs/mngr_latchkey/imbue/mngr_latchkey/extensions/services.json`](../../../libs/mngr_latchkey/imbue/mngr_latchkey/extensions/services.json)
and is read directly at desktop-client runtime by
`imbue.mngr_latchkey.services_catalog.ServicesCatalog`. Each service maps
to a *list* of scope entries (a single service may expose more than one
detent scope).
Each entry has the shape:

* `scope` -- the detent scope schema the service owns; used as the rule
  key in `latchkey_permissions.json` and as the value the agent puts
  in its permission request's `scope` field.
* `display_name` -- human-readable label shown in the dialog header.
* `permissions` -- granular detent permission schemas the dialog offers
  as checkboxes. The catch-all ``any`` schema is prepended client-side
  as an available option (the gateway file does not list it); the
  dialog never pre-checks it, but the user can opt into it explicitly.

The minds desktop client caches the response in-process on first access
so each request renders without re-fetching. To add a new service,
edit `services.json` in the gateway extension package (see its README).
Schemas must already exist in detent; minds does not register custom
schemas.

## Agent-side responsibilities

Agents are expected to:

* Detect the three blocked outcomes from the gateway response.
* POST a permission request to the gateway's `permission-requests`
  extension (`POST /permission-requests` with `scope`, `permissions`,
  and `rationale`).
* Stop the turn and wait. The agent will receive an `mngr message` from
  the desktop with the decision and can decide whether to retry.

The detection-and-wait logic for Claude Code lives in the
`forever-claude-template` repository's latchkey skill, not in this
monorepo.

## Peer-mind operations

Agents can create new peer minds (siblings, not children) and follow
their creation through to completion via the gateway's `minds-api-proxy`
extension (see "Minds API access through the gateway" above) -- the
same path agents already use for the `/api/v1/...` REST API:

```bash
# Spawn a peer.
curl -X POST "$LATCHKEY_GATEWAY/minds-api-proxy/api/create-agent" \
  -H "X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
  -H "X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
  -H 'Content-Type: application/json' \
  -d '{
    "git_url": "https://github.com/example/template",
    "launch_mode": "DOCKER",
    "ai_provider": "SUBSCRIPTION"
  }'

# Poll its creation status (use the ``agent_id`` from the spawn response).
curl "$LATCHKEY_GATEWAY/minds-api-proxy/api/create-agent/creation-XXXX/status" \
  -H "X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
  -H "X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"

# Stream creation logs (server-sent events): same shape with -N and the
# .../logs path.
```

The proxy injects the agent-invisible `Authorization: Bearer
<MINDS_API_KEY>` header itself (overwriting anything the agent
supplied), and the desktop client's `/api/create-agent`,
`/api/create-agent/{id}/status`, and `/api/create-agent/{id}/logs`
endpoints accept that central key as an alternative to the browser
session cookie. The agent never sees the key.

Unlike the always-granted per-agent notifications route, the peer-spawn
paths are gated behind a user-facing grant: **a detent scope named
`minds`, with three named permissions**, is materialized inline in
every per-agent `latchkey_permissions.json` baseline (defined in
`libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py`). The scope
schema gates `domain=latchkey-self.invalid` AND `path` under
`/minds-api-proxy/api/create-agent`; the named permissions
`minds-create`, `minds-status`, and `minds-logs` each match a specific
`(method, path)` pair. The scope is not added to detent's built-in
catalog so the rule stays self-contained and minds owns the schema
definition. Future operations (destroy / list peer minds) will be
added as additional named permissions under the same scope.

Note that `latchkey services info minds` reports
`credentialStatus=unknown` -- `minds` is a catalog scope served by a
gateway extension, not a registered latchkey service with its own
stored credential. The grant flow treats `unknown` as "proceed": only
`missing` / `invalid` credentials trigger the auth setup path.

### First-spawn flow

On a fresh install, the first peer-spawn attempt hits the standard
permission-request dialog:

1. Agent POSTs to `$LATCHKEY_GATEWAY/minds-api-proxy/api/create-agent`.
2. Gateway evaluates the request against the agent's permissions file.
   No `minds` rule yet, so detent denies. Gateway returns 403 with
   `Request not permitted by the user.`.
3. Agent submits `POST /permission-requests` with `scope=minds` and a
   rationale ("I want to spawn a peer mind to explore X").
4. Desktop client surfaces a card titled "Peer minds". The user picks
   either `any` (one-click full grant) or any combination of
   `minds-create`, `minds-status`, and `minds-logs`.
5. Desktop writes e.g. `{"minds": ["any"]}` into the agent's permissions
   file via the gateway's `permissions` extension.
6. Agent retries the request. Detent now matches; the `minds-api-proxy`
   extension injects the bearer header and forwards. Minds validates
   the key and returns `{"agent_id": "<creation_id>", "status": "INITIALIZING"}`.
7. Agent polls `GET .../api/create-agent/<creation_id>/status` and may
   stream `GET .../api/create-agent/<creation_id>/logs` -- both are
   covered by the same grant -- until `agent_id` is populated. The new
   mind then appears in the UI agent list as a peer of the creating
   mind.

Subsequent spawns from the same agent go through without a dialog --
the rule persists in the host's permissions file. Revoke by deleting
the rule from the same dialog.

### Adding the schema to existing installs

Agents created before this feature shipped have a host permissions
file without the inline `minds` scope schemas, so detent cannot match
the rule even after the user grants it. `minds run` runs an idempotent
migration at startup that injects the `minds` scope schema plus the
three named-permission schemas into any existing
`hosts/<host_id>/latchkey_permissions.json` that is missing them. The
migration happens *before* the gateway is restarted to avoid a race
with the `permissions.mjs` extension.
