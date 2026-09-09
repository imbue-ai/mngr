# mngr-latchkey

Latchkey gateway management for [mngr](https://github.com/imbue-ai/mngr).

This package owns the lifecycle of a single shared `latchkey gateway`
subprocess and the per-agent state that points the gateway at each
agent's own permissions file. It ships both as a Python library
and as a `mngr` CLI plugin that registers the `mngr latchkey`
command group.

## CLI

Once `imbue-mngr-latchkey` is installed, `mngr` discovers the plugin
via the standard entry-point mechanism and exposes:

```
mngr latchkey forward            # long-running supervisor: gateway + reverse tunnels
mngr latchkey create-agent-env   # emit LATCHKEY_* env vars + opaque permissions handle as JSON
mngr latchkey link-permissions   # swing the opaque handle's symlink to the canonical host path
mngr latchkey register-agent     # register an agent so it can reach the Minds API proxy
mngr latchkey admin-jwt          # mint a wildcard permissions-override JWT for the gateway
mngr latchkey gateway-info       # print the running gateway's URL + listen password as JSON
```

`mngr latchkey forward` spawns the shared gateway eagerly on startup
and stops it on `SIGINT`/`SIGTERM` (coupled lifetime). Any in-flight
agents lose their gateway endpoint until the next `mngr latchkey
forward` is started; the per-host permissions files survive across
restarts.

While running, the supervisor also health-checks the shared gateway
subprocess: if it dies mid-session it is respawned on its original
port (so agent reverse tunnels and the published gateway port stay
valid), rather than leaving agent traffic silently broken until the
supervisor itself is restarted.

### Wiring a new agent using the CLI interface

```sh
# In one terminal, leave the supervisor running for the lifetime of the agents.
export MNGR_LATCHKEY_DIRECTORY=~/.minds/latchkey
mngr latchkey forward

# In another terminal, per host:
export MNGR_LATCHKEY_DIRECTORY=~/.minds/latchkey
mngr latchkey create-agent-env > /tmp/lk.json
OPAQUE_PATH=$(jq -r .opaque_permissions_path /tmp/lk.json)
HOST_ENV_ARGS=$(jq -r '.env | to_entries[] | "--host-env \(.key)=\(.value)"' /tmp/lk.json)

# Substitute your preferred mngr create invocation here. The latchkey
# env is passed via --host-env so every agent on the new host inherits
# the same gateway wiring.
CREATED=$(mngr create my-template $HOST_ENV_ARGS --format json)
HOST_ID=$(echo "$CREATED" | jq -r .host_id)
AGENT_ID=$(echo "$CREATED" | jq -r .agent_id)

# Finalize the opaque permissions handle: swing its symlink to the
# canonical host-keyed permissions path.
mngr latchkey link-permissions --host-id "$HOST_ID" --opaque-path "$OPAQUE_PATH"

# Register this agent for the host so it can reach the Minds API proxy.
# The baseline rule rejects every ``/minds-api-proxy/api/v1/agents/<id>/...``
# request whose ``<id>`` is not in the host's allowed-agent enum, so
# every minds agent that wants to call the Minds API must be registered
# here. Idempotent: re-running for an already-registered agent is a no-op.
# A host with a machine of its own (a remote workspace whose gateway was
# provisioned from this computer) is also handed the updated file, since
# its gateway enforces its own copy; the command fails if it cannot be.
mngr latchkey register-agent --host-id "$HOST_ID" --agent-id "$AGENT_ID"
```

## Settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via the matching env vars
(`MNGR_LATCHKEY_DIRECTORY`, `MNGR_LATCHKEY_BINARY`) and per-invocation
CLI flags (`--latchkey-directory`, `--latchkey-binary`). Precedence is
CLI flag > env var > settings.toml > built-in default.

## Logs

`mngr latchkey forward` writes its logs under the plugin data directory
(`<latchkey_directory>/mngr_latchkey/`):

- `events.jsonl` -- the supervisor's **structured** log, written via the
  standard mngr/minds JSONL sink: one flat JSON object per line with a
  nanosecond `timestamp`, `level`, `message`, and source location,
  size-rotated (rotated copies `events.jsonl.<timestamp>`, oldest
  pruned). Read this when you need to observe timing. The shared
  `latchkey gateway` subprocess's output is routed through the same log
  (each line at `DEBUG`, prefixed with `[latchkey gateway]`), so it is
  timestamped and rotated too rather than living in a separate unrotated
  file.

- `latchkey_forward.log` -- the raw stdout/stderr capture of the detached
  supervisor process. Its file descriptor is handed straight to the
  process, so it cannot be rotated mid-write; instead the supervisor is
  spawned with `--quiet`, so in steady state it logs nothing here (all
  logging goes to `events.jsonl`). This file therefore stays effectively
  empty and only ever captures rare startup-failure output (Click errors
  or a pre-logging traceback) that never reaches the structured log -- so
  it is the place to look if the supervisor dies before it starts logging.

  Each spawn appends a `<timestamp> === spawning ... ===` marker before
  handing the descriptor over. The child's own lines cannot be stamped
  from the parent (it writes to the descriptor directly), so the marker is
  what dates whatever follows it, letting a traceback here be lined up
  against the timestamped logs uploaded alongside it. Spawn time is also
  the only moment the file can safely be rotated -- no child holds the
  descriptor yet -- so an oversized capture (one left by an older build,
  or by a child crash-looping before its logging is configured) is rotated
  there, once it passes 10MB, to `latchkey_forward.log.<timestamp>`, keeping
  only the newest rotation. Without that the file is append-only for the life
  of the install, and is gzipped and re-uploaded with every bug report.

## Troubleshooting

### Two forwards for one latchkey directory

The forward is supervisor-managed, and exactly one should be running per
latchkey directory: each holds an exclusive lock on its own directory for its
whole life, so a second one for the same directory refuses to start. Forwards
for *different* directories are expected, so tell them apart by the
`--latchkey-directory` in each title:

```sh
ps -eo pid,args | grep '[m]ngr latchkey forward'
kill <stray-pid>
```

`SIGTERM` runs the supervisor's teardown, which stops its `mngr observe` child,
its reverse tunnels and the shared gateway subprocess, so killing the
supervisor leaves nothing behind to clean up by hand.

## Error reporting (Sentry)

`mngr latchkey forward` can report errors to Sentry. It is **off by default** and
configured entirely via `MNGR_LATCHKEY_SENTRY_*` environment variables (the
`MNGR_LATCHKEY_` prefix distinguishes `mngr latchkey` from the upstream core
`latchkey` project). The supervisor owns no Sentry project / environment
definitions: it receives concrete values as strings, which the embedder resolves
and passes in.

The **infrastructure** (which project, how the build is tagged) is snapshotted
into the daemon's environment when it is spawned:

- `MNGR_LATCHKEY_SENTRY_DSN` -- the Sentry DSN to report to.
- `MNGR_LATCHKEY_SENTRY_ENVIRONMENT` -- the Sentry environment label (e.g.
  `production`, `staging`, `development`).
- `MNGR_LATCHKEY_SENTRY_RELEASE` / `MNGR_LATCHKEY_SENTRY_GIT_SHA` -- the release
  version and git SHA events are tagged with.
- `MNGR_LATCHKEY_SENTRY_S3_BUCKET` -- the S3 bucket to upload the supervisor's
  logs (`events.jsonl`, rotated copies, `latchkey_forward.log`) and a captured
  traceback to. Empty / unset means there is no bucket, so nothing is uploaded.

Sentry initializes whenever `DSN`, `ENVIRONMENT`, `RELEASE`, and `GIT_SHA` are all
present (run standalone without them, it simply does nothing). They are required
together: the supervisor has no fallback of its own.

The **consent** -- whether to send reports at all (log/traceback attachments ride
along with reports) -- is read live, not snapshotted, so the embedder can toggle
it on a running daemon without respawning it:

- `MNGR_LATCHKEY_SENTRY_CONSENT_FILE` -- path to a JSON file
  (`{"report_unexpected_errors": bool}`) that the embedder writes and rewrites
  whenever the user changes their consent. The daemon reads it on every event, so
  a grant/revoke takes effect immediately. An absent/unreadable file means
  reporting is off.

Events are tagged with the `mngr-latchkey-forward` service name so they are
distinguishable from other Imbue Python processes that report to the same
projects. When the minds desktop client spawns the supervisor it sets all of
these automatically -- resolving the DSN / environment / bucket from its own
Sentry settings and maintaining the consent file from the user's error-reporting
settings.

## Desktop egress

Some destinations block the datacenter IP ranges a remote workspace's VPS sits
in, so a request has to leave from the user's own machine to be accepted at all.
Workspaces ask for that by *prefixing* the target URL, because latchkey's
gateway routes on the request path alone -- a header cannot divert a request
that already looks like `/gateway/<url>`.

`prepare_agent_latchkey` publishes the prefix to use as `MINDS_VIA_DESKTOP_URL_PREFIX`
in every workspace's env:

| Workspace | Value | Why |
|---|---|---|
| VPS gateway | `https://latchkey-self.invalid/via-desktop` | `latchkey curl` recognizes the reserved `latchkey-self.invalid` host and rewrites such URLs onto the gateway's own origin, so the wrapped URL arrives as `/via-desktop/<target>` and the forwarding extension hands it to the desktop |
| desktop gateway | `""` (empty) | the gateway already runs on the user's machine, so a plain request is already desktop egress and a prefix would only add a hop |

In-workspace tooling therefore concatenates the value without branching on
topology, and gets the right behavior in both. It is always set (empty rather
than absent) so tooling can tell "minds configured no prefix" apart from "this
workspace predates the feature".

The name is `MINDS_*` rather than `LATCHKEY_*` even though the value is a
latchkey URL. A workspace's env names each var after the tool that *reads* it
(`LATCHKEY_GATEWAY` for latchkey, `MNGR_HOST_DIR` for the inner mngr), and
latchkey never reads this one -- it only receives the concatenated result as a
URL argument. With no single reader to name it after, it takes the name of the
authority that decides the value: minds, which alone knows the topology.

```sh
latchkey curl "$MINDS_VIA_DESKTOP_URL_PREFIX/https://api.example.com/v1/thing"
```

This grants nothing: the agent baseline opens the *route*, and what may be
reached through it is decided by the ordinary per-service, per-account rules,
which the desktop evaluates against the real target URL after unwrapping. A
service the user has granted is reachable both ways with one grant, and a
service they have not granted is reachable neither way -- so desktop egress
never appears as its own consent prompt.

## Machine stores

Credentials belong to the machine that uses them: the user's computer owns one
credential store (shared by every local host), and each remote host's VPS owns
its own. So that a remote host's credentials can be *read* with the ordinary
offline latchkey commands -- and so a browser sign-in has somewhere to land
before it is handed over -- every remote host gets a **machine store**: its
existing per-host directory, made into a usable `LATCHKEY_DIRECTORY`. It is a
scratch pad, refilled from the machine (`MachineCredentials.refresh`) whenever
what it says is about to be shown or acted on, never trusted between times:

```
<latchkey_directory>/mngr_latchkey/hosts/<host_id>/
    credentials.json.enc          this machine's credentials      (owned)
    data-format-version           the mirror's own format stamp   (owned)
    latchkey_permissions.json     the machine's policy, cached    (owned)
    machine_encryption_key        the machine's own key           (owned)
    machine_gateway_password      the machine's own password      (owned)
    permissions.json           -> latchkey_permissions.json
    config.json                -> the desktop's config.json
    browser_state.json.enc     -> the desktop's browser state
    encryption_key             -> the desktop's encryption key
    last-daily-count           -> the desktop's usage-ping stamp
```

The last of those is shared for a plainer reason than the rest: it rate-limits
latchkey's once-a-day usage ping, which is about the user, so an unshared stamp
would make an ordinary offline read against each machine store ping once a day
per remote host.

`imbue.mngr_latchkey.remote._mirror` owns that layout. Two properties are worth
knowing:

- **A mirror is held under the desktop's key**, whatever key the mirrored
  machine uses for its own store. Upstream encrypts the browser session with
  the same per-directory key as the credential store, so this is what lets every
  machine store share one browser session (which keeps signing a second machine
  in to a service down to a consent click rather than a full re-login).
  Transfers re-encrypt at the boundary instead: what is shipped to a machine is
  encrypted with *its* key (`Latchkey.export_credentials_subset`'s
  `destination_key`, handed to the CLI on stdin so it never reaches `argv`).

- **Each machine's key is recorded in its machine store -- as a mirror, not the
  truth.** A machine holds its own key only in RAM (provisioning writes it to a
  tmpfs file, deliberately never to the disk beside the encrypted store), and
  that RAM copy is authoritative while it exists: any of the user's computers
  may have provisioned the machine, so each desktop's provisioning pass *adopts*
  the key the machine is already running under rather than deciding one, and
  keeps the durable copy so a rebooted machine (whose tmpfs is wiped) can be
  handed its key back. Only a machine that is not running a key, with none
  recorded here, gets one decided: a fresh key when it holds no credential
  store; the desktop's key when its store verifiably opens under it (a machine
  provisioned by a build that predates per-machine keys, whose agents may carry
  a permissions-override JWT the gateway validates with a key derived from it);
  and otherwise -- a store written under a key held only by a computer that is
  gone -- the store is abandoned and a fresh key minted, because signing in
  again is possible and waiting for a computer that may never return is not.

- **A machine's gateway listen password is recorded the same way, and is
  adopted for a blunter reason.** It is the password the workspaces on that
  machine present (`LATCHKEY_GATEWAY_PASSWORD`), and their host env file is
  written once, at `mngr create`, with nothing to rewrite it afterwards. So the
  value the creating computer chose is fixed for the machine's whole life: a
  second computer that wrote its own here would answer every request those
  workspaces make with a 401. Provisioning therefore adopts what the machine is
  running under, falls back to the record here for a machine whose tmpfs a
  reboot wiped, and only seeds its own value (`Latchkey.derive_gateway_password`,
  which is also what it bakes into the workspaces it creates) into a machine
  neither is true of. The password the *desktop* gateway listens on is a
  separate secret, and the one place the two used to be the same is described
  under [Remote desktop-gateway proxy
  extension](#remote-desktop-gateway-proxy-extension).

- **A machine store is not a plugin root.** `Latchkey.plugin_data_dir` would
  resolve to a nested `mngr_latchkey/` underneath it, and `initialize()` would
  rewrite the shared `config.json` through its link, so only the
  credential/service-introspection subset of `Latchkey` may be pointed at one.

`imbue.mngr_latchkey.remote.credentials` is how a machine is reached.
`MachineCredentials` is built for the duration of one exchange -- the caller
opens the machine's outer host, does what it came to do, and lets both go --
and every method costs a single remote command: `connect_service`,
`disconnect_account`, `set_permissions` and `connect_service_with_permissions`
push, and `refresh` reads the machine's credentials *and* its policy back in
one go. Nothing is queued: an exchange either succeeds before its caller
returns or raises `RemoteGatewayError`, so an embedder (the minds desktop app)
can block a user's click on it and report what the machine said.

`refresh` also settles which side wins, and for both halves the answer is the
machine. The credentials are obviously its own -- only it can rotate the tokens
it holds. The policy is its own for a less obvious reason: the user may have
more than one computer, and any of them can push a grant. A desktop that
treated its own copy as the truth would quietly revert what another computer
granted, so what the machine holds is adopted here instead. The one write
`refresh` makes toward the machine is the seed: a machine with no policy at all
gets this desktop's copy, which is how a freshly provisioned gateway stops
permitting everything.

What keeps that safe is that the copy here is never edited *without* being
pushed. Every writer of `latchkey_permissions.json` -- a UI toggle, a grant, an
agent registration, a recovery repair -- pushes the result to the machine in the
same breath, and a push that fails is reported (to the user when there is one to
report to, to the log otherwise) rather than left behind as a local edit that a
later refresh would silently discard.

## Permissions config

The package owns the `latchkey_permissions.json` schema (a subset of
detent's rule format). Per-host edits go through the gateway's
bundled `permissions` extension (see [Gateway HTTP extensions](#gateway-http-extensions));
only the deny-all default, the admin file, and the per-agent opaque
baseline are written directly via `imbue.mngr_latchkey.store.save_permissions`.

### Per-account grants

Third-party service access is granted **per latchkey account**, not per
service. Latchkey (>= 3.2.0) tells detent which account's credentials it
injected into a request (as `customMetadata.account`; the unnamed default
account is the empty string), and detent (>= 1.11.0) can compose schemas, so
each grant is a rule keyed `<scope>:<account>` backed by a generated schema
that intersects the built-in scope with that account:

```json
{
  "rules": [{ "slack-api:hynek@imbue-ai": ["slack-read-all"] }],
  "schemas": {
    "slack-api:hynek@imbue-ai": {
      "allOf": [
        { "$ref": "#/$defs/slack-api" },
        {
          "properties": {
            "customMetadata": {
              "type": "object",
              "properties": { "account": { "const": "hynek@imbue-ai" } },
              "required": ["account"]
            }
          },
          "required": ["customMetadata"]
        }
      ]
    }
  }
}
```

Detent stops at the first rule whose *scope* matches, and a request made with
another account does not match this one, so per-account rules simply stack.

The `<scope>:<account>` **name is only a naming convention** -- a stable,
human-readable identifier -- and is never parsed: both a detent scope name and
an account may legitimately contain a colon. Everything that needs to know what
a rule grants inspects the *schema structure* instead (the `$ref` to the base
scope next to the `customMetadata.account` gate).

The name is still required to be *unique* per (scope, account) pair, since the
gateway merges rules by key, so the scope half is percent-escaped (`%` -> `%25`,
then `:` -> `%3A`) before the two are joined. That makes the mapping injective
whatever either half contains -- and it is the identity for every scope name the
catalog ships, so keys read exactly as above. The account is the last field and
is never escaped.
`imbue.mngr_latchkey.account_scopes` is the single owner of both sides of that
structure: `build_account_grant` composes a grant (key + permissions + backing
schema) and `list_account_grants` / `resolve_account_scope` /
`resolved_schema_names` read grants back.
`ServicesCatalog.list_service_account_grants` layers the catalog on top, which
is what every consumer (the minds connectors page, the permission dialog's
pre-check, the revoke paths, and VPS credential sync) actually calls. The
gateway's `permission_requests` extension carries a JavaScript copy of the two
*generating* helpers (it computes a pending request's effect in-process), guarded
against drift by `account_scopes_test.py`; nothing on the JavaScript side reads
grants back.

Minds' own gateway-self scopes (`latchkey-self`, `minds-api-proxy-*`) stay
account-agnostic: latchkey attaches no account metadata to requests an
extension serves, so an account-gated schema would never match them.

## Data-format changes

The plugin has no data-format migration mechanism. A permissions file is
whatever the machine that owns it holds, and the shape it is written in is the
one the installed code produces: `LatchkeyPermissionsConfig` is `extra="ignore"`,
so a file carrying keys this build does not model still loads, and those keys
disappear the next time the file is saved.

That self-healing only covers *extra keys*. It does not cover a change that
moves data between rule keys, and there is nothing left that would rewrite such
a file. So a permissions shape change is now a breaking change across desktops:
since a refresh adopts whatever the machine holds (see "Machine stores" above),
a newer desktop's push is read verbatim by an older one. Any future mechanism
for this has to put the version *on the wire* alongside the policy, not only in
a file on disk -- a local-only stamp cannot help a policy that arrives from
another computer.

---

# Reference

The sections below are deeper detail for power users, front-end authors,
and embedders. Most callers only need the CLI above.

## Gateway HTTP extensions

`mngr latchkey forward` drops three desktop-only `.mjs` extensions into
`<latchkey-directory>/extensions/`. All expose plain HTTP endpoints
on the gateway's listen port and authenticate the caller via two
headers:

* `X-Latchkey-Gateway-Password: <password>` -- the gateway listen
  password from `mngr latchkey gateway-info`.
* `X-Latchkey-Gateway-Permissions-Override: <jwt>` -- a JWT minted
  for the permissions file you want the gateway to evaluate the
  request against. For full access to both extensions, use the JWT
  from `mngr latchkey admin-jwt`.

A shell client would typically wire these up once:

```sh
ADMIN_JWT=$(mngr latchkey admin-jwt)
eval "$(mngr latchkey gateway-info | jq -r '@text "GATEWAY_URL=\(.url); GATEWAY_PASSWORD=\(.password)"')"
auth=(-H "X-Latchkey-Gateway-Password: $GATEWAY_PASSWORD" -H "X-Latchkey-Gateway-Permissions-Override: $ADMIN_JWT")
```

### `permission-requests` extension

A pending-permission queue. Agents submit a request when they hit a
blocked service; UIs (the minds desktop client, your own front-end)
consume the stream and approve/delete on resolution.

* `POST /permission-requests` with body
  `{"agent_id": "...", "rationale": "...", "type": "...", "payload": {...}}`.
  Two `type` values are accepted:
  * `"predefined"` -- detent scope/permission grant for one signed-in
    account of the service, with payload
    `{"scope": "...", "permissions": ["...", ...], "account": "..."}`.
    The scope must be one named in the bundled `services.json` catalog,
    and each permission must be either one the catalog lists for that
    scope or the catch-all `any`. `account` is the latchkey account the
    grant applies to (the unnamed default account is the empty string);
    it is optional, and an agent that does not know which account to use
    omits it. A request with no account has an **empty** `effect` -- it
    can only be resolved by a client that names the chosen account in the
    approve override body (see below), which is what the minds dialog
    does after the user picks or signs one in.
  * `"file-sharing"` -- single-file access through the `minds-api-proxy`
    extension, with payload `{"path": "<absolute-path>"}`. The path
    must be absolute and free of `..` segments.

  The extension generates a `request_id` server-side, stores the
  caller-supplied fields plus the `target` permissions.json (taken
  from the extension context) and a precomputed `effect`
  (`{rules?, schemas?}`) that an approval would splice into
  `target`, and returns the full persisted record. Available to
  agents.
* `GET /permission-requests` returns the current queue as
  newline-delimited JSON. Each line carries the full persisted
  shape. Add `?follow=true` to keep the connection open and stream
  every newly-POSTed request as it arrives. Available to the admin.
* `POST /permission-requests/approve/<request_id>` approves the
  named request: the extension reads it, splices its `effect` into
  its `target` permissions.json (creating the file if missing,
  merging rules by scope key and schemas by name), then removes the
  pending request file. Returns `200` with `{request_id, target,
  applied}` where `applied` is the freshly-rewritten permissions
  file. Available to the admin.

  An optional JSON body overrides what the approval grants, recomputing
  the effect from the user's choices: `{"account": "...",
  "permissions": [...]}` for a `predefined` request (the permission list
  is optional), `{"path": "..."}` for `file-sharing`, and
  `{"permissions": [...], "target_workspace_id": ...}` for `workspace`.
* `DELETE /permission-requests/<request_id>` removes a single pending
  request without applying its effect. UIs call this on deny so a
  fresh `?follow=true` consumer never sees the resolved request
  again. Available to the admin.

Pending requests are stored as one JSON file per request under
`<latchkey-directory>/permission_requests/v3/`. The `v3` segment is
the on-disk schema version; future shape changes get a new directory
rather than trying to migrate files in place (`v3` introduced the
per-account `predefined` payload).

### `minds-api-proxy` extension

Transparent HTTP reverse proxy from the gateway to an embedder-supplied
"Minds API" base URL.

* `ANY /minds-api-proxy` forwards to `<minds-api>/`.
* `ANY /minds-api-proxy/<rest>...` forwards to
  `<minds-api>/<rest>...`, preserving the inbound method, query
  string, headers (minus hop-by-hop entries and the gateway-internal
  password / permissions-override headers), and body. The upstream
  response status, headers, and body stream straight back.

The upstream base URL is read from the
`LATCHKEY_EXTENSION_MINDS_API_URL` env var on every request. If the
var is unset/empty/unparseable the proxy responds 503 with a JSON
error body. There is no in-process cache to invalidate: an embedder
that needs to repoint the proxy at a new upstream simply respawns
the gateway (or the `mngr latchkey forward` supervisor that owns it)
with a fresh value for the env var.

The proxy authenticates *to* the upstream Minds API on behalf of the
agent. When `LATCHKEY_EXTENSION_MINDS_API_KEY` is set, the proxy
overwrites the inbound `Authorization` header with
`Bearer <LATCHKEY_EXTENSION_MINDS_API_KEY>` before forwarding. Agents
therefore never see the key, and an agent that tries to spoof an
`Authorization` header has its value dropped on the floor. When the
env var is unset, the inbound `Authorization` value is forwarded
unchanged (useful for tests / local fixtures that do not bother
stubbing the key; the upstream will simply 401 the request).

Other than the `Authorization` overwrite, the extension performs no
authentication of its own beyond the gateway's normal permission
check (against the synthetic `latchkey-self.invalid` URL). Restricting
which paths an agent can reach through the proxy is therefore a job
for the agent's `latchkey_permissions.json`.

### Remote desktop-gateway proxy extension

Remote workspaces expose the VPS-resident gateway at the same
`http://127.0.0.1:1989` URL local workspaces use. Third-party requests terminate
there so the VPS can inject the credentials its own store holds. The VPS gateway
loads one dedicated `desktop_gateway_proxy.mjs` extension for the endpoint
families whose state remains on the user's computer: `/permissions`,
`/permission-requests`, and `/minds-api-proxy` (including all subpaths). It
forwards those requests to the desktop gateway over a desktop-to-VPS reverse
tunnel, authenticating that hop with the desktop's own gateway password and a
dedicated desktop-target permissions JWT -- both of which *replace* whatever the
caller sent, since the caller's password authenticates it to the VPS gateway and
its override would let it choose the policy the desktop evaluates it against.
Native VPS requests carry no override and are authorized by the machine's own
`~/.latchkey/permissions.json` (seeded at provisioning, then rewritten by the
full permission snapshot the desktop pushes on every edit).

Those two desktop-owned secrets are handed to the extension as *paths* into the
machine's tmpfs secrets directory (`LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PASSWORD_FILE`,
`LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PERMISSIONS_OVERRIDE_FILE`), and it reads
both afresh on every request it proxies. Both belong to whichever of the user's
computers is currently on the other end of the tunnel -- the password is that
gateway's own, and the JWT is signed by that computer's encryption key and names
a path on its disk -- so both change when the user moves to another computer,
while the machine (and the workspace it serves) keeps running. Every
provisioning pass overwrites the files, and the per-request read is what makes
the new computer's values take effect without restarting the VPS gateway. It
also means the machine's *own* listen password is a distinct secret that
provisioning adopts rather than rewrites, so a new computer never locks the
workspaces out of their own gateway (see [Machine stores](#machine-stores)).
When neither file is there -- a rebooted machine awaiting its next provisioning
pass -- the desktop-owned routes answer HTTP 503 saying so, while third-party
calls, which need neither secret, keep working.

One computer at a time is assumed. Two of the user's computers running at once
contend for both the desktop-to-VPS tunnel (whose VPS port only one can bind)
and these files (which the last provisioning pass wins), so the desktop-owned
routes can end up presenting one computer's secrets to the other's gateway and
failing with 401 until the computer holding the tunnel provisions again.

The same extension serves `/via-desktop/<absolute-target-url>`, which asks for a
*third-party* request to leave from the user's machine rather than from the VPS
-- some destinations block datacenter IP ranges outright. That family is
forwarded with its prefix swapped for `/gateway/`, so it lands on the desktop
gateway's own outbound proxy and the desktop needs no extension of its own; the
target is required to be an absolute `http(s)` URL and is sliced off the raw
request URL, so it reaches the third party byte-identical to what the caller
sent. Credentials are injected, and the permission check runs, on the desktop
against the same host permissions file the proxy already targets, so this route
reaches nothing a direct request could not. See [Desktop
egress](#desktop-egress) for how a workspace asks for it.

The workspace therefore always has one gateway URL and one agent-side skill.
If the user's computer is offline, third-party calls through the VPS gateway
continue to work, while desktop-owned extension routes fail with a clear HTTP
502 response. Calls carrying an *expiring* credential -- an OAuth connection or
Zoom -- keep working only until its access token runs out (typically an hour):
the VPS gateway is launched with `LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1`, so
only the desktop renews those, and it does so from a periodic loop that stops
with the machine. Static tokens are unaffected.

Workspaces created *before* this one-gateway rollout still carry a
permissions-override JWT in their host env file, naming a desktop-side opaque
handle path that upstream latchkey resolves before dispatching anything
(answering HTTP 400 when the named file is absent). Provisioning used to symlink
that path at the VPS `permissions.json` on every reconcile; those symlinks live
on the VPS and stay valid, so the shim itself is gone.

### `permissions` extension

Reads and edits a detent permissions file at a caller-supplied path.
The gateway is launched with the environment variable
`LATCHKEY_EXTENSION_PERMISSIONS_ROOT` pointing at this package's data
directory; any `path` query parameter that resolves outside that
root is rejected with HTTP 403.

* `GET /permissions?path=<file>` returns the full permissions file.
* `GET /permissions/available` returns the full permission catalog as
  a JSON object keyed by raw service name. Each value is an array of
  scope entries (a single service may expose more than one scope), each
  with the shape `{"scope": "<schema_name>", "display_name": "...",
  "description": "...", "permissions": [{"name": "<schema_name>",
  "description": "..."}, ...]}`. The scope-level `description` and each
  permission's `description` carry detent's per-schema `$comment`
  summaries (both optional).
* `GET /permissions/available/<service_name>` returns the permission
  catalog entries for `<service_name>` (e.g. `slack`, `google-gmail`)
  as an array, using the same value shape, or 404 if the service is
  unknown. The catch-all `any` permission is always injected at index 0
  of every scope's `permissions` array, so a caller can always
  request unrestricted access under a known scope. This endpoint
  is backed by a `services.json` file (keyed by raw service name)
  that ships alongside the extension; the path query parameter
  is not consulted.
* `GET /permissions/rules?path=<file>&rule_key=<scope>` returns the
  rule for `<scope>`, or 404 if absent.
* `POST /permissions/rules?path=<file>&rule_key=<key>` with the body
  `{"permissions": ["slack-read-all", ...], "schemas": {"<name>": {...}}}`
  adds or replaces the rule for `<key>`. `schemas` is optional and is
  merged by name into the file's `schemas` object; everything else in
  the file is preserved verbatim. The target file (and any missing
  parent directories, e.g. `hosts/<host_id>/`) is created if it does
  not yet exist.

  The extension never synthesizes schemas and never interprets
  `<key>`, so a caller whose key is not a name detent already knows (a
  built-in schema, or one already defined in the file) **must** define
  it here. That is how per-account grants are written: their key names a
  generated schema composed by
  `imbue.mngr_latchkey.account_scopes.build_account_grant`, which owns
  that shape (see [Per-account grants](#per-account-grants)).
* `DELETE /permissions/rules?path=<file>&rule_key=<scope>` removes
  the named rule.

The `services.json` catalog is generated from detent's built-in request
schemas; do not edit it by hand. Regenerate it against a detent checkout
with:

```sh
uv run python libs/mngr_latchkey/scripts/generate_services_json.py \
  --detent-root /path/to/detent
```

Display names and the service ordering are editorial metadata detent does
not carry; they live as curated constants in that script.

Services hidden from agents (`core.HIDDEN_BUILTIN_SERVICES`, currently just
`notion`) are left out of the catalog: latchkey never injects their
credentials, so an entry for one would only offer grants that can never be
used. The generator skips them, so the catalog and the gateway's
`settings.hideBuiltinServices` cannot drift apart.

`services.json` also carries minds' own *additional* (custom) services --
ones detent has no schemas for, currently `claude.ai`. Their definitions are
hand-maintained in `imbue/mngr_latchkey/additional_services.json` (a
`display_name`, a `registration`, the single Detent `scope` it exposes with an
inline scope `schema`, and its grantable `permissions`, each with an inline
`schema`), and the generator *folds their catalog entries into* `services.json`.
That way every reader of the catalog -- `ServicesCatalog` and both gateway
extensions -- works from one file in one shape and never has to know which of
the two sources a service came from.

`registration` is written in **latchkey's own shape**: it is the object that
lands verbatim under `registeredServices.<name>` in latchkey's `config.json`,
so a service is described here exactly as `latchkey services register` would
persist it. Nothing on the Python side models or validates its contents --
latchkey owns that schema and checks it when it loads the config -- so adding
a service, or picking up a field a later latchkey adds, is a data-only change.

For `claude-ai` that is a `baseApiUrl` plus a `loginUrl` and a `loginFlow`,
which give it a `latchkey auth browser` sign-in (latchkey ships these generic
flows so a service outside its builtin catalog can still be signed into).
`claude-ai` uses `cookie-capture`, which finishes once the named cookies have
been set and stores them as a `Cookie` header -- for claude.ai, the single
`sessionKey` cookie, scoped by `cookieUrl` to claude.ai itself because sign-in
may start on another host. A service registered with only a `baseApiUrl` can
be authenticated by hand with `latchkey auth set` instead.

Because a custom scope is not a detent builtin, its schemas have to reach the
gateway's permission check. They are **inlined into every permissions file minds
writes**: the agent baseline (`baseline_permissions.ADDITIONAL_SERVICE_SCHEMAS`)
carries them, and `agent_setup.reconcile_baseline_permissions` refreshes them on
files that already exist, so the bundled definition always wins over a stale
copy. Granting a custom scope is then a plain rule write against a file that
already defines the scope.

Inlining rather than sharing one file via detent's `include` is deliberate.
Detent resolves an `include` relative to the directory of the file that
references it, and a host's permissions file is reachable through several
directories -- its canonical `hosts/<host_id>/` path, the opaque handle in
`permissions/` that a desktop workspace's JWT names, and
`~/.latchkey/permissions.json` on a VPS. A shared file would have to be copied
next to each of them, and an include that fails to resolve fails the *whole*
permission check for that host, not just the rule that needed it. A
self-contained file has nothing to resolve.

`imbue.mngr_latchkey.additional_services` is the single Python chokepoint for
the file. It exposes the registration entries, the merged detent schemas the
baseline inlines, and the catalog projection the generator folds into
`services.json`. No gateway extension reads it -- they only read
`services.json`.

The registration entries are minds' half of latchkey's own `config.json`:
`core.merge_minds_latchkey_config` read-merges them into the file's
`registeredServices` block (alongside `settings.hideBuiltinServices`) rather
than shelling out to `latchkey services register`, which cannot update a
registration that already exists. That merge runs for **every** gateway that
serves minds agents -- the desktop one at `initialize()` and each gateway spawn,
and a VPS one during remote provisioning. It has to: the registration is what
lets a gateway resolve a request to a custom service at all, so a VPS holding
the synchronized credentials but not the registration would silently never
inject them.

A typical end-to-end shell flow:

```sh
# Stream pending requests as they come in.
curl -N "${auth[@]}" "$GATEWAY_URL/permission-requests?follow=true"

# Grant the agent slack-read-all for one Slack account on its host's
# permissions file. Grants are per account, so the rule names a generated
# schema that gates the built-in slack-api scope on that account -- and the
# caller, not the gateway, defines it.
HOST_PERMS=$MNGR_LATCHKEY_DIRECTORY/mngr_latchkey/hosts/$HOST_ID/latchkey_permissions.json
RULE_KEY='slack-api:hynek@imbue-ai'
curl -X POST "${auth[@]}" -H "Content-Type: application/json" \
  -d '{"permissions": ["slack-read-all"],
       "schemas": {"slack-api:hynek@imbue-ai": {"allOf": [
         {"$ref": "#/$defs/slack-api"},
         {"properties": {"customMetadata": {"type": "object",
            "properties": {"account": {"const": "hynek@imbue-ai"}},
            "required": ["account"]}},
          "required": ["customMetadata"]}]}}}' \
  "$GATEWAY_URL/permissions/rules?path=$HOST_PERMS&rule_key=$RULE_KEY"

# Clear the pending request now that it has been resolved.
curl -X DELETE "${auth[@]}" "$GATEWAY_URL/permission-requests/$REQUEST_ID"
```

## Embedding

Embedders (such as the minds desktop client) typically want a single
detached ``mngr latchkey forward`` supervisor that survives embedder
restarts and adopts the existing one instead of double-spawning. The
:class:`LatchkeyForwardSupervisor` does exactly that:

```python
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

supervisor = LatchkeyForwardSupervisor(
    mngr_binary="/path/to/mngr",          # default: ``mngr`` on PATH
    latchkey_binary="/path/to/latchkey",  # default: ``latchkey`` on PATH
    latchkey_directory=root_dir,
)
supervisor.ensure_running()  # idempotent; spawns or adopts as needed
# ... do whatever the embedder does ...
# Optional: ``supervisor.stop()`` to terminate the detached process and
# tear down the gateway. Omitting this leaves the supervisor running
# detached, which is what minds does so the gateway survives a
# desktop-client restart.
```

## Python API

Every CLI subcommand is a thin wrapper around the library; the library
remains importable for embedders such as the minds desktop client.

```python
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.agent_setup import (
    LatchkeyGatewayLocation,
    prepare_agent_latchkey,
    finalize_host_permissions,
)
from imbue.mngr_latchkey.discovery import (
    LatchkeyDiscoveryHandler,
    LatchkeyDestructionHandler,
)
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager

latchkey = Latchkey(
    latchkey_binary="/path/to/latchkey",  # default: "latchkey" on PATH
    latchkey_directory=root_dir,
)
latchkey.initialize()

# (a) Pre-create env vars + opaque permissions handle for a new host.
setup = prepare_agent_latchkey(
    latchkey,
    is_tunneled=True,
    gateway_location=LatchkeyGatewayLocation.VPS,
)
# setup.env: LATCHKEY_GATEWAY[_PASSWORD,_DISABLE_COUNTING]
# Desktop-gateway setups also include LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE.
# LATCHKEY_GATEWAY is always http://127.0.0.1:1989 for tunneled workspaces.
# Discovery realizes the desktop or VPS location selected before creation.
# setup.opaque_permissions_path: pass to finalize_host_permissions later

# ... mngr create returns the canonical host id ...

# (b) Point the opaque handle at the canonical host permissions path.
finalize_host_permissions(latchkey, setup.opaque_permissions_path, host_id)
# Raises LatchkeyStoreError on failure -- callers decide whether to abort
# or just surface a warning.

# (c) Plug the discovery and destruction handlers into your agent
# discovery stream so reverse tunnels are opened on discovery and
# closed on destruction.
tunnel_manager = SSHTunnelManager()
tunnel_manager.start_reverse_tunnel_health_check()
on_discovered = LatchkeyDiscoveryHandler(
    latchkey=latchkey, tunnel_manager=tunnel_manager, concurrency_group=cg
)
on_destroyed = LatchkeyDestructionHandler(tunnel_manager=tunnel_manager)
```

The `latchkey_directory` is used both as the upstream `LATCHKEY_DIRECTORY`
for spawned `latchkey` subprocesses and as the root of this package's own
metadata subdirectory (`<latchkey_directory>/mngr_latchkey/`, accessible
via `Latchkey.plugin_data_dir`).

### Storing user-supplied credentials

Services with no browser sign-in report an example of the command that
stores their credentials, each value the caller must supply written as an
angle-bracketed placeholder (`LatchkeyServiceInfo.set_credentials_example`,
e.g. `latchkey auth set-nocurl aws <access-key-id> <secret-access-key>`).
`imbue.mngr_latchkey.credential_commands` turns such an example into a
fillable form and back into a runnable command, so an embedder can collect
the values in its own UI instead of sending the user to a terminal:

```python
from imbue.mngr_latchkey.credential_commands import (
    build_credential_command_argv,
    parse_credential_command_example,
)

# Raises CredentialCommandError when the example is not a latchkey command,
# or has no placeholders (nothing to ask the user for).
parsed = parse_credential_command_example(service_info.set_credentials_example)
# parsed.parameters: one (name, label) pair per placeholder, to render as inputs

argv = build_credential_command_argv(
    parsed,
    {"access-key-id": "...", "secret-access-key": "..."},
    account,  # "" for latchkey's unnamed default account
)
is_success, detail = latchkey.auth_set_credentials("aws", argv)
```

The argv carries the user's secrets, so it is passed to the subprocess as a
list (never a shell string) and is never logged. `--account` is a *global*
latchkey option and is therefore placed before the subcommand rather than
after the example's own arguments. `auth set` stores whatever it is handed,
so callers should re-read `services_info` afterwards to find out whether
the credentials are actually usable.
