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

## Permissions config

The package owns the `latchkey_permissions.json` schema (a subset of
detent's rule format). Per-host edits go through the gateway's
bundled `permissions` extension (see [Gateway HTTP extensions](#gateway-http-extensions));
only the deny-all default, the admin file, and the per-agent opaque
baseline are written directly via `imbue.mngr_latchkey.store.save_permissions`.

---

# Reference

The sections below are deeper detail for power users, front-end authors,
and embedders. Most callers only need the CLI above.

## Gateway HTTP extensions

`mngr latchkey forward` drops three `.mjs` extensions into
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
  * `"predefined"` -- detent scope/permission grant, with payload
    `{"scope": "...", "permissions": ["...", ...]}`. The scope must be
    one named in the bundled `services.json` catalog, and each
    permission must be either one the catalog lists for that scope or
    the catch-all `any`.
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
* `DELETE /permission-requests/<request_id>` removes a single pending
  request without applying its effect. UIs call this on deny so a
  fresh `?follow=true` consumer never sees the resolved request
  again. Available to the admin.

Pending requests are stored as one JSON file per request under
`<latchkey-directory>/permission_requests/v2/`. The `v2` segment is
the on-disk schema version; future shape changes get a new directory
rather than trying to migrate files in place.

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
* `POST /permissions/rules?path=<file>&rule_key=<scope>` with a JSON
  body of permission-schema names (`["any"]`,
  `["slack-read-all", ...]`, ...) adds or replaces the rule for
  `<scope>`. Everything in the file other than the matching rule is
  preserved verbatim. The target file (and any missing parent
  directories, e.g. `hosts/<host_id>/`) is created if it does not yet
  exist.
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

A typical end-to-end shell flow:

```sh
# Stream pending requests as they come in.
curl -N "${auth[@]}" "$GATEWAY_URL/permission-requests?follow=true"

# Grant the agent slack-read-all on its host's permissions file.
HOST_PERMS=$MNGR_LATCHKEY_DIRECTORY/mngr_latchkey/hosts/$HOST_ID/latchkey_permissions.json
curl -X POST "${auth[@]}" \
  -H "Content-Type: application/json" -d '["slack-read-all"]' \
  "$GATEWAY_URL/permissions/rules?path=$HOST_PERMS&rule_key=slack-api"

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
setup = prepare_agent_latchkey(latchkey, is_tunneled=True)
# setup.env: LATCHKEY_GATEWAY[_SECONDARY,_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]
#   LATCHKEY_GATEWAY_SECONDARY (tunneled mode only) is the agent's URL for the
#   per-VPS gateway: http://127.0.0.1:<INNER_PORT>
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
