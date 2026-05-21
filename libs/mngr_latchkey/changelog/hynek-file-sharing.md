- Latchkey gateway ships a new bundled `minds-api-proxy` extension that
  transparently reverse-proxies requests under
  `/extensions/minds-api-proxy` to the minds desktop client's bare-origin
  "Minds API". The upstream URL is read at request time from the
  `LATCHKEY_EXTENSION_MINDS_API_URL` environment variable, and is
  published to the detached `mngr latchkey forward` supervisor (via the
  new `LatchkeyForwardSupervisor.extra_env`) on every `minds run`
  startup, so the proxy always points at the live Minds API port even
  when minds re-binds on restart. The extension responds 503 when the
  env var is not configured; requests still go through the gateway's
  normal permission check.
- The Latchkey gateway's `permission-requests` extension grows a typed
  request schema and a new approve endpoint:
  - `POST /permission-requests` now takes
    `{agent_id, rationale, type, payload}` instead of the legacy flat
    `{scope, permissions, ...}` shape. The `type` field is
    `"predefined"` (payload `{scope, permissions}`) or `"file-sharing"`
    (payload `{path}`, absolute-only, no `..` segments).
  - Each pending request is persisted with the additional `target`
    (the extension's per-request `permissionsConfigPath`) and `effect`
    (a precomputed `{rules?, schemas?}` patch) fields. Pending requests
    live under `<latchkey-directory>/permission_requests/v2/` -- the
    `v2` segment is the on-disk schema version so future shape changes
    can land in a fresh directory rather than trying to migrate files
    in place.
  - `POST /permission-requests/approve/<request_id>` is new. It reads
    the pending request, merges its `effect.rules` (union by scope key)
    and `effect.schemas` (overwrite by name) into the stored `target`
    permissions.json (creating it if missing), and deletes the pending
    request file. Returns the fresh permissions file in the response
    body.
  - The legacy `DELETE /permission-requests/<id>` continues to remove
    a pending request without applying its effect; the minds desktop
    client uses it for the deny path.
- The `file-sharing` permission effect was rewritten to target the new
  WebDAV mount that replaced `/api/v1/file-server` on the minds side:
  - The scope schema (`minds-file-server`) now matches any URL under
    `/extensions/minds-api-proxy/api/v1/files` via a `pattern`
    (`^/extensions/minds-api-proxy/api/v1/files(/.*)?$`) instead of a
    `const`. The synthetic gateway host (`latchkey-self.invalid`) is
    unchanged.
  - The per-file permission schema now pins `path` (the URL path) to
    the WebDAV URL for the requested file -- the WebDAV mount serves
    each absolute path at the URL
    `/extensions/minds-api-proxy/api/v1/files<absolute_path>`, so a
    grant for `/home/user/foo.txt` matches exactly
    `/extensions/minds-api-proxy/api/v1/files/home/user/foo.txt`. The
    legacy `queryParams.path` constraint is gone (WebDAV identifies
    the file in the URL path, not via a query parameter).
  - The allowed `method` enum grew from `GET` / `POST` to the full set
    of WebDAV verbs needed to read, write, query, lock, copy, and
    delete the file: `GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`,
    `PROPFIND`, `PROPPATCH`, `MKCOL`, `COPY`, `MOVE`, `LOCK`,
    `UNLOCK`.
  - The wire shape and dedup behaviour of the request itself are
    unchanged: the agent still POSTs
    `{type: "file-sharing", payload: {path: "<absolute>"}}` and the
    per-file permission schema name is still
    `minds-file-server-<sha256(path)[:32]>`, so re-approving an
    existing grant remains idempotent.
