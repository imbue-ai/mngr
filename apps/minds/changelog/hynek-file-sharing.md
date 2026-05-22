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
  (a precomputed `{rules?, schemas?}` patch) fields. Pending requests
  live under `<latchkey-directory>/permission_requests/v2/` -- the
  `v2` segment is the on-disk schema version so future shape changes
  can land in a fresh directory rather than trying to migrate files
  in place.
* `POST /permission-requests/approve/<request_id>` is new. It reads
  the pending request, merges its `effect.rules` (union by scope
  key) and `effect.schemas` (overwrite by name) into the stored
  `target` permissions.json (creating it if missing), and deletes
  the pending request file. Returns the fresh permissions file in
  the response body.
* The legacy `DELETE /permission-requests/<id>` continues to remove
  a pending request without applying its effect; the minds desktop
  client uses it for the deny path.
* The `file-sharing` effect now targets the WebDAV mount described
  below: the scope schema matches any URL under
  `/extensions/minds-api-proxy/api/v1/files` via a `pattern`, and the
  per-file permission schema const-matches the URL path itself
  (`/extensions/minds-api-proxy/api/v1/files<absolute_path>`). The
  legacy `queryParams.path` constraint is gone.
* File-sharing requests now carry a required `access` field on the
  payload (`READ` / `WRITE`). `READ` unlocks the non-mutating WebDAV
  verbs only (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`); `WRITE` is a
  strict superset that also unlocks the single-path mutating verbs
  `PUT`, `DELETE`, `PROPPATCH`, `MKCOL`, `LOCK`, `UNLOCK`. `COPY` and
  `MOVE` are intentionally excluded -- both carry a second path in
  the WebDAV `Destination` header that the per-file permission schema
  cannot constrain, so granting either would let an agent write to a
  different file in the share than the one actually shared. Per-file
  permission schemas embed the access mode in their name
  (`minds-file-server-read-<hash>` / `minds-file-server-write-<hash>`)
  so the two grants are independent. The minds approval dialog shows
  a green "read-only" or amber "read & write" badge and explains
  what the agent will be allowed to do; the granted / denied
  notification text reflects the mode as well.

The minds desktop client's latchkey-permission handler code was
reorganised so the two permission request types now live as siblings
under a single `imbue.minds.desktop_client.latchkey.handlers`
package: `.predefined` (the existing catalog-backed flow, moved from
`latchkey/permissions.py`) and `.file_sharing` (moved from
`latchkey/file_sharing.py`). Their shared helpers (`MngrMessageSender`
and the Jinja-template renderers) live alongside them in the same
package. The file-sharing approval dialog now uses the same Jinja
template + Tailwind base (`templates/permissions.html`) and visual
style as the predefined dialog instead of a hand-written HTML page.

The minds desktop client side learns to render and resolve both
request types:

* `LatchkeyPermissionRequestEvent` was renamed to
  `LatchkeyPredefinedPermissionRequestEvent` to mirror the wire
  `type=predefined` and to distinguish it from the new file-sharing
  event (both flow through Latchkey).
* A new `LatchkeyFileSharingPermissionRequestEvent` (and
  accompanying `FileSharingGrantHandler`) renders a single yes/no
  dialog per absolute file path. Approval calls
  `POST /permission-requests/approve/<id>` on the gateway; denial
  uses the existing DELETE path. There is no UI to revoke or edit
  an existing file-sharing grant -- the user has to edit
  `latchkey_permissions.json` by hand for that, for now.
* `LatchkeyGatewayClient` gains an `approve_permission_request`
  method. The `StreamedPermissionRequest` model carries the new
  wire shape (`request_type` + `payload` + `target` + `effect`).
  `payload` is typed directly as the `PredefinedRequestPayload |
  FileSharingRequestPayload` union (pydantic's smart-union mode
  resolves the two disjoint shapes at decode time), and `effect` is
  typed as a `PermissionEffect` model with `rules` and `schemas`
  fields. Consumers dispatch via `isinstance` on `payload` rather
  than re-validating the dict at the call site.

The Minds REST API ships a new WebDAV file-server mount at
`/api/v1/files`, backed by [`wsgidav`](https://wsgidav.readthedocs.io/)
wrapped in [`a2wsgi`](https://github.com/abersheeran/a2wsgi). Two
share roots are exposed:

* the current user's home directory (`Path.home()`); and
* `/tmp`.

Each share is mounted at its on-disk path so the outward URL mirrors
the absolute path one-to-one: `/home/<user>/foo.txt` is reached at
`/api/v1/files/home/<user>/foo.txt`, `/tmp/blob.bin` at
`/api/v1/files/tmp/blob.bin`. Any standard WebDAV verb works (`GET`,
`PUT`, `PROPFIND`, `DELETE`, ...); paths outside the two shares are
not served. The HTML directory browser is disabled.

The mount uses the same per-agent Bearer-token authentication as the
rest of `/api/v1/`: a thin ASGI wrapper verifies
`Authorization: Bearer <api_key>` against `find_agent_by_api_key` and
401s before any request reaches the filesystem; WsgiDAV itself runs
anonymous. The mount is reachable from agents through the
`minds-api-proxy` Latchkey extension.
