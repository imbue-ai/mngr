## mngr-latchkey + minds: switch permission management to the latchkey 2.9.0 gateway extensions

### Summary

Latchkey 2.9.0 ships two new gateway extensions that this branch wires
into `mngr_latchkey` and the minds desktop client:

- `permission_requests.mjs` -- per-process pending-permission queue.
  Agents `POST /permission-requests` when they hit a blocked service;
  the desktop client consumes `GET /permission-requests?follow=true`
  to learn about new requests and `DELETE /permission-requests/<id>`
  to clear them once granted or denied.
- `permissions.mjs` -- a `permissions.json` editor that operates on any
  file path inside `LATCHKEY_EXTENSION_PERMISSIONS_ROOT`. Used by the
  desktop client to apply per-host permission grants via
  `POST /permissions/rules?path=<host_file>&rule_key=<scope>`.

Both extensions are bundled in `imbue-mngr-latchkey` and dropped into
`<LATCHKEY_DIRECTORY>/extensions/` automatically every time `mngr
latchkey forward` spawns the shared gateway.

### `imbue-mngr-latchkey`

- `LATCHKEY_MIN_VERSION` bumped from 2.8.0 to 2.9.0.
- New extension files at
  `imbue/mngr_latchkey/extensions/{permission_requests,permissions}.mjs`,
  rewritten from the originally-supplied drafts:
    * `permissions.mjs` now takes the target file path and rule key
      via the `?path=` and `?rule_key=` query params. It requires the
      `LATCHKEY_EXTENSION_PERMISSIONS_ROOT` env var (set by
      `Latchkey._spawn_gateway` to the plugin data dir) and refuses
      any path that resolves outside it.
    * `permission_requests.mjs` no longer accepts a caller-supplied
      `request_id`; the extension generates one server-side (a
      UUID-shaped hex string) and returns it in the POST response.
- `Latchkey.create_admin_permissions_jwt()` -- materializes
  `<plugin_data_dir>/latchkey_admin_permissions.json` (idempotent,
  with the wildcard rule `{"any": ["any"]}`) and returns a cached
  JWT pointing at it. Calling code uses this JWT in the
  `X-Latchkey-Gateway-Permissions-Override` header when it needs
  full access to the gateway's extension endpoints.
- New `mngr latchkey admin-jwt` CLI subcommand wraps the above and
  prints the JWT on stdout for shell-driven workflows.
- New on-disk `LatchkeyGatewayInfo` record at
  `<plugin_data_dir>/latchkey_gateway.json`, written by `mngr
  latchkey forward` immediately after the gateway binds its port and
  deleted on shutdown. Lets non-spawning processes (the minds
  desktop client) discover the gateway URL + listen password without
  having to mint either themselves.

### Minds desktop client

- `cli/run.py` now blocks on `_wait_for_gateway_info` before the
  FastAPI app is built, mints an admin JWT, and constructs a
  `LatchkeyGatewayClient` shared by every code path that talks to
  the gateway extensions.
- New `PermissionRequestsConsumer` daemon thread streams
  `GET /permission-requests?follow=true` and feeds each pending
  request into the existing `RequestInbox`. The legacy
  `events.jsonl` callback now ignores `LATCHKEY_PERMISSION` lines
  because the extension owns that flow; non-latchkey
  `PERMISSIONS` events still go through the JSONL channel
  unchanged.
- `LatchkeyPermissionGrantHandler` applies grants via the new
  `permissions` extension (`POST /permissions/rules?path=...&rule_key=...`)
  and clears the pending gateway record via `DELETE
  /permission-requests/<id>` on both grant and deny.
- New `gateway_client.py`, `permission_requests_consumer.py`, and
  `testing.py` modules support the above; corresponding unit-test
  files exercise the HTTP wire shape and the streaming/translation
  paths.

### Compatibility

Agents that still post `LATCHKEY_PERMISSION` request events via the
old `events.jsonl` channel will no longer reach the minds inbox.
Migrating agents to the gateway-side `POST /permission-requests`
endpoint is a follow-up; agents will additionally need their
per-host baseline permissions to grant `latchkey-self` access so the
gateway accepts the POST.
