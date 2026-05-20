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
   agent appends a `LatchkeyPermissionRequestEvent` to
   `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl` with the latchkey
   service name and a one-paragraph rationale, then ends its turn and goes
   idle.
4. **Desktop notifies the user.** The desktop client tails the agent's
   request events file via `mngr events --follow`, adds a card to the
   right-side requests inbox panel, and surfaces a notification.
5. **User opens the dialog.** Clicking the card opens
   `/requests/<event_id>`, which renders a single-scope permission dialog:
   * The list of [Detent](https://github.com/imbue-ai/detent) permission
     schemas the user can grant for that scope, fetched from the latchkey
     gateway's `GET /permissions/available` endpoint and cached in
     process for the lifetime of the desktop client.
   * The detent ``any`` schema (matches every request inside the scope) is
     prepended as the first checkbox so the user can opt into unrestricted
     access if they want. It is **not** pre-checked.
   * The dialog pre-checks the union of (a) permissions already granted
     for that scope on the agent's host and (b) the permissions the agent
     declared in the request event. Approving without changes grants
     exactly that union; ticking more broadens it, unticking narrows or
     revokes. The dialog therefore doubles as a revocation UI.
   * The Approve button stays disabled while zero boxes are checked,
     so if the agent submitted an empty ``permissions`` tuple and the
     user has no prior grants for the scope, the user must actively
     pick something before approving.
6. **User approves.** The desktop client:
   1. Runs `latchkey services info <service>` to read `credentialStatus`,
      `authOptions`, and `setCredentialsExample`.
   2. If credentials are not `valid` and the service advertises a
      `browser` auth option (or latchkey reports no `authOptions` at all,
      treated as the legacy fallback), runs `latchkey auth browser <service>`
      synchronously; cancellation/failure produces an `AUTH_FAILED` outcome.
   3. If credentials are not `valid` and the service does not advertise a
      `browser` auth option (e.g. Coolify, where `authOptions = ["set"]`),
      the grant is **refused** and the request stays pending. The dialog
      shows the `setCredentialsExample` returned by latchkey (or a
      generic fallback) and asks the user to run it in a terminal. A
      subsequent Approve click re-runs `latchkey services info` and
      proceeds normally once credentials are valid.
   4. Atomically rewrites the agent's `latchkey_permissions.json` so the gateway
      enforces the chosen schemas on the next request.
   5. Appends a `GRANTED` (or `AUTH_FAILED`) response event to
      `~/.minds/events/requests/events.jsonl`.
   6. Sends the agent a plain-English `mngr message` describing the
      decision; the agent wakes up and decides whether to retry.
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
and is fetched at desktop-client runtime via the gateway's
`GET /permissions/available` endpoint. Each entry has the shape:

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
their creation through to completion through the same latchkey curl
pipeline they use for third-party services:

```bash
# Spawn a peer.
latchkey curl -X POST 'http://127.0.0.1:9100/api/create-agent' \
  -H 'Content-Type: application/json' \
  -d '{
    "git_url": "https://github.com/example/template",
    "launch_mode": "LOCAL",
    "ai_provider": "SUBSCRIPTION"
  }'

# Poll its creation status (use the ``agent_id`` from the spawn response).
latchkey curl 'http://127.0.0.1:9100/api/create-agent/creation-XXXX/status'

# Stream creation logs (server-sent events).
latchkey curl -N 'http://127.0.0.1:9100/api/create-agent/creation-XXXX/logs'
```

Latchkey injects the agent-invisible `Authorization: Bearer ...` header
itself. The agent must never see the token directly. Three pieces wire
this together; all are set up by `minds run` at startup:

* **The `minds` service is registered with latchkey** against the
  desktop client's base URL (`http://127.0.0.1:<port>`). `minds run`
  calls `latchkey services register minds --base-api-url ...` and
  re-registers when the port changes across restarts.
* **A bearer token is persisted** at `<minds-data-dir>/auth/api_token`
  with mode 0o600 and stored in latchkey's encrypted credential store
  via `latchkey auth set minds -H 'Authorization: Bearer <token>'`. The
  desktop client's `/api/create-agent`, `/api/create-agent/{id}/status`,
  and `/api/create-agent/{id}/logs` endpoints accept the token as an
  alternative to the browser session cookie.
* **A detent scope named `minds`, with three named permissions**, is
  materialized inline in every per-agent `latchkey_permissions.json`
  baseline (defined in
  `libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py`). The scope
  schema gates `domain=127.0.0.1` AND `path` under `/api/create-agent`;
  the named permissions `minds-create`, `minds-status`, and `minds-logs`
  each match a specific `(method, path)` pair. The scope is not added
  to detent's built-in catalog so the rule stays self-contained and
  minds owns the schema definition. Future operations (destroy / list
  peer minds) will be added as additional named permissions under the
  same scope.

Because the `latchkey gateway` itself spawns curl on the desktop host
(see "End-to-end flow" above), `127.0.0.1` in the request URL resolves
to the desktop's loopback regardless of which compute backend the
calling agent runs on. The string `http://127.0.0.1:<minds-port>` is
identical for Docker, LIMA, Vultr, and IMBUE_CLOUD agents.

### First-spawn flow

On a fresh install, the first peer-spawn attempt hits the standard
permission-request dialog:

1. Agent runs `latchkey curl -X POST 'http://127.0.0.1:9100/api/create-agent' ...`.
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
6. Agent retries the curl. Detent now matches; gateway looks up the
   `minds` service, injects the bearer header, and forwards. Minds
   validates the token and returns `{"agent_id": "<creation_id>", "status": "CLONING"}`.
7. Agent polls `GET /api/create-agent/<creation_id>/status` and may
   stream `GET /api/create-agent/<creation_id>/logs` -- both are
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
