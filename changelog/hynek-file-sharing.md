Latchkey gateway ships a new bundled `minds-api-proxy` extension that
transparently reverse-proxies requests under `/extensions/minds-api-proxy`
to the minds desktop client's bare-origin "Minds API". The upstream URL
is read at request time from the `LATCHKEY_EXTENSION_MINDS_API_URL`
environment variable, and is published to the detached
`mngr latchkey forward` supervisor (via the new
`LatchkeyForwardSupervisor.extra_env`) on every `minds run` startup, so
the proxy always points at the live Minds API port even when minds
re-binds on restart. The extension responds 503 when the env var is not
configured; requests still go through the gateway's normal permission
check.

The Latchkey gateway's `permission-requests` extension grows a typed
request schema and a new approve endpoint:

* `POST /permission-requests` now takes `{agent_id, rationale, type,
  payload}` instead of the legacy flat `{scope, permissions, ...}`
  shape. The `type` field is `"predefined"` (payload
  `{scope, permissions}`) or `"file-sharing"` (payload `{path}`,
  absolute-only, no `..` segments).
* Each pending request is persisted with the additional `target`
  (the extension's per-request `permissionsConfigPath`) and `effect`
  (a precomputed `{rules?, schemas?}` patch) fields. The on-disk
  schema version bumps from `permission_requests/v1` to
  `permission_requests/v2`; the gateway ignores stray v1 files.
* `POST /permission-requests/approve/<request_id>` is new. It reads
  the pending request, merges its `effect.rules` (union by scope
  key) and `effect.schemas` (overwrite by name) into the stored
  `target` permissions.json (creating it if missing), and deletes
  the pending request file. Returns the fresh permissions file in
  the response body.
* The legacy `DELETE /permission-requests/<id>` continues to remove
  a pending request without applying its effect; the minds desktop
  client uses it for the deny path.

The minds desktop client side learns to render and resolve both
request types:

* `LatchkeyPermissionRequestEvent` continues to drive the existing
  per-permission checkbox dialog for `predefined` requests.
* A new `FileSharingPermissionRequestEvent` (and accompanying
  `FileSharingGrantHandler`) renders a single yes/no dialog per
  absolute file path. Approval calls
  `POST /permission-requests/approve/<id>` on the gateway; denial
  uses the existing DELETE path. There is no UI to revoke or edit
  an existing file-sharing grant -- the user has to edit
  `latchkey_permissions.json` by hand for that, for now.
* `LatchkeyGatewayClient` gains an `approve_permission_request`
  method. The `StreamedPermissionRequest` model carries the new
  wire shape (`request_type` + `payload` + `target` + `effect`)
  with typed convenience methods to extract the payload as a
  `PredefinedRequestPayload` / `FileSharingRequestPayload`.

The Minds REST API ships a new `/api/v1/file-server` endpoint for
reading, listing, stat-ing, and writing files on the desktop host:

* `GET /api/v1/file-server?path=<absolute>&operation=READ` streams a
  file's bytes back to the caller.
* `GET /api/v1/file-server?path=<absolute>&operation=LIST` returns a
  JSON directory listing with per-entry type, size, and mtime.
* `GET /api/v1/file-server?path=<absolute>&operation=STAT` returns
  the same metadata for a single path (files, directories, and
  symlinks classified via `lstat`).
* `POST /api/v1/file-server?path=<absolute>` writes the raw request
  body to disk. Defaults to refusing with `409 Conflict` when the
  target already exists; pass `overwrite=true` to replace an existing
  regular file. Missing parent directories are created on demand.

The endpoint uses the same per-agent Bearer-token authentication as
the rest of `/api/v1/` and is reachable from agents through the
`minds-api-proxy` Latchkey extension.
