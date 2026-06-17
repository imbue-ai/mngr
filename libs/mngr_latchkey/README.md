# mngr-latchkey

Latchkey gateway management for [mngr](https://github.com/imbue-ai/mngr).

This package owns the lifecycle of a single shared `latchkey gateway`
subprocess and the per-agent state that points the gateway at each
agent's permissions file. It ships as a `mngr` CLI plugin that
registers the `mngr latchkey` command group.

## CLI

Once `imbue-mngr-latchkey` is installed, `mngr` discovers it via the
standard entry-point mechanism and exposes:

```
mngr latchkey forward            # long-running supervisor: gateway + reverse tunnels
mngr latchkey create-agent-env   # emit LATCHKEY_* env vars + opaque permissions handle as JSON
mngr latchkey link-permissions   # point the opaque handle's symlink at the canonical host path
mngr latchkey register-agent     # register an agent so it can reach the Minds API proxy
mngr latchkey admin-jwt          # mint a wildcard permissions-override JWT for the gateway
mngr latchkey gateway-info       # print the running gateway's URL + listen password as JSON
```

`mngr latchkey forward` spawns the shared gateway on startup and stops
it on `SIGINT`/`SIGTERM`. While it is down, in-flight agents lose their
gateway endpoint until the next `forward` is started; the per-host
permissions files survive across restarts.

### Wiring a new agent

```sh
# In one terminal, leave the supervisor running for the lifetime of the agents.
export MNGR_LATCHKEY_DIRECTORY=~/.minds/latchkey
mngr latchkey forward

# In another terminal, per host:
export MNGR_LATCHKEY_DIRECTORY=~/.minds/latchkey
mngr latchkey create-agent-env > /tmp/lk.json
OPAQUE_PATH=$(jq -r .opaque_permissions_path /tmp/lk.json)
HOST_ENV_ARGS=$(jq -r '.env | to_entries[] | "--host-env \(.key)=\(.value)"' /tmp/lk.json)

# Pass the latchkey env via --host-env so every agent on the new host
# inherits the same gateway wiring. Substitute your own mngr create call.
CREATED=$(mngr create my-template $HOST_ENV_ARGS --format json)
HOST_ID=$(echo "$CREATED" | jq -r .host_id)
AGENT_ID=$(echo "$CREATED" | jq -r .agent_id)

# Point the opaque permissions handle at the canonical host path.
mngr latchkey link-permissions --host-id "$HOST_ID" --opaque-path "$OPAQUE_PATH"

# Register the agent so it can reach the Minds API proxy. The baseline
# rule rejects any /minds-api-proxy/api/v1/agents/<id>/... request whose
# <id> is not registered for the host. Idempotent.
mngr latchkey register-agent --host-id "$HOST_ID" --agent-id "$AGENT_ID"
```

## Settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via env vars (`MNGR_LATCHKEY_DIRECTORY`,
`MNGR_LATCHKEY_BINARY`) and per-invocation CLI flags
(`--latchkey-directory`, `--latchkey-binary`). Precedence is
CLI flag > env var > settings.toml > built-in default.

## Logs

`mngr latchkey forward` writes logs under
`<latchkey_directory>/mngr_latchkey/`:

- `events.jsonl` -- the supervisor's structured log (one JSON object per
  line, size-rotated). The shared gateway subprocess's output is routed
  here too, prefixed with `[latchkey gateway]`. Read this to observe
  timing.
- `latchkey_forward.log` -- raw stdout/stderr of the detached supervisor.
  In steady state it stays empty (the supervisor runs `--quiet`); it only
  captures rare startup-failure output that never reaches the structured
  log, so check it if the supervisor dies before it starts logging.

## Permissions config

The package owns the `latchkey_permissions.json` schema (a subset of
detent's rule format). Per-host permission edits are applied by the
running gateway's bundled `permissions` HTTP extension; the gateway
authenticates each request via two headers:

- `X-Latchkey-Gateway-Password` -- the listen password from
  `mngr latchkey gateway-info`.
- `X-Latchkey-Gateway-Permissions-Override` -- a JWT for the permissions
  file to evaluate against. For full access, use the JWT from
  `mngr latchkey admin-jwt`.

The gateway exposes three extensions on its listen port:

- `permissions` -- read and edit a detent permissions file (`GET`/`POST`/
  `DELETE /permissions/rules`), and browse the available service/permission
  catalog (`GET /permissions/available`).
- `permission-requests` -- a pending-request queue. Agents `POST` a
  request when blocked; UIs `GET` the queue (with `?follow=true` to
  stream) and approve or delete it on resolution.
- `minds-api-proxy` -- a transparent reverse proxy from the gateway to an
  embedder-supplied Minds API base URL, injecting the upstream auth token
  so agents never see it.

A typical end-to-end shell flow (grant an agent a permission and clear the
pending request):

```sh
ADMIN_JWT=$(mngr latchkey admin-jwt)
eval "$(mngr latchkey gateway-info | jq -r '@text "GATEWAY_URL=\(.url); GATEWAY_PASSWORD=\(.password)"')"
auth=(-H "X-Latchkey-Gateway-Password: $GATEWAY_PASSWORD" -H "X-Latchkey-Gateway-Permissions-Override: $ADMIN_JWT")

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

## Limitations

While `mngr latchkey forward` is not running, agents have no gateway
endpoint. Per-host permissions files persist across restarts, but
in-flight agents are cut off until the supervisor comes back up.
