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

mngr latchkey link-permissions --host-id "$HOST_ID" --opaque-path "$OPAQUE_PATH"
```

### Settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via the matching env vars
(`MNGR_LATCHKEY_DIRECTORY`, `MNGR_LATCHKEY_BINARY`) and per-invocation
CLI flags (`--latchkey-directory`, `--latchkey-binary`). Precedence is
CLI flag > env var > settings.toml > built-in default.

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
from imbue.mngr_latchkey.ssh_tunnel import SSHTunnelManager

latchkey = Latchkey(
    latchkey_binary="/path/to/latchkey",  # default: "latchkey" on PATH
    latchkey_directory=root_dir,
)
latchkey.initialize()

# (a) Pre-create env vars + opaque permissions handle for a new host.
setup = prepare_agent_latchkey(latchkey, is_tunneled=True)
# setup.env: LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]
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

## Permissions config

The package owns the `latchkey_permissions.json` schema (a subset of
detent's rule format). Per-host edits go through the gateway's
bundled `permissions` extension (see below); only the deny-all
default, the admin file, and the per-agent opaque baseline are
written directly via `imbue.mngr_latchkey.store.save_permissions`.

## Gateway HTTP extensions

`mngr latchkey forward` drops two `.mjs` extensions into
`<latchkey-directory>/extensions/`. Both expose plain HTTP endpoints
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
consume the stream and DELETE on resolution.

* `POST /permission-requests` with body
  `{"agent_id": "...", "scope": "...", "permissions": ["...", ...], "rationale": "..."}`.
  The extension generates a `request_id` server-side and returns the
  full record. Available to agents.
* `GET /permission-requests` returns the current queue as
  newline-delimited JSON. Add `?follow=true` to keep the connection
  open and stream every newly-POSTed request as it arrives.
  Available to the admin.
* `DELETE /permission-requests/<request_id>` removes a single pending
  request. UIs call this on grant or deny so a fresh `?follow=true`
  consumer never sees the resolved request again. Available to
  the admin.

Pending requests are stored as one JSON file per request under
`<latchkey-directory>/permission_requests/v1/`. The `v1` segment is
part of the on-disk schema version, so any pre-v1 files that happen
to live in the parent directory are ignored.

### `permissions` extension

Reads and edits a detent permissions file at a caller-supplied path.
The gateway is launched with the environment variable
`LATCHKEY_EXTENSION_PERMISSIONS_ROOT` pointing at this package's data
directory; any `path` query parameter that resolves outside that
root is rejected with HTTP 403.

* `GET /permissions?path=<file>` returns the full permissions file.
* `GET /permissions/available/<service_name>` returns the permission
  catalog entry for `<service_name>` (e.g. `slack`, `google-gmail`)
  as a `{"scope": {"schema_name": "...", "display_name": "..."},
  "permissions": ["...", ...]}` object, or 404 if the service is
  unknown. The catalog is backed by a `services.json` file (keyed by
  raw service name) that ships alongside the extension; the path
  query parameter is not consulted.
* `GET /permissions/rules?path=<file>&rule_key=<scope>` returns the
  rule for `<scope>`, or 404 if absent.
* `POST /permissions/rules?path=<file>&rule_key=<scope>` with a JSON
  body of permission-schema names (`["any"]`,
  `["slack-read-all", ...]`, ...) adds or replaces the rule for
  `<scope>`. Everything in the file other than the matching rule is
  preserved verbatim.
* `DELETE /permissions/rules?path=<file>&rule_key=<scope>` removes
  the named rule.

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
