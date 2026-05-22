- Latchkey gateway ships a new bundled `minds-api-proxy` extension that
  transparently reverse-proxies requests under
  `/minds-api-proxy` to the minds desktop client's bare-origin
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
  - The effect no longer mints its own scope schema. The rule now
    attaches the per-file permission to the pre-existing `latchkey-self`
    scope from the agent baseline (defined in `agent_setup.py`), which
    already matches any request whose `domain` is
    `latchkey-self.invalid`. The per-file permission schema is what
    restricts the grant to a single WebDAV URL + verb set; the scope
    just identifies which rule list the permission belongs to.
  - The per-file permission schema now pins `path` (the URL path) to
    the WebDAV URL for the requested file -- the WebDAV mount serves
    each absolute path at the URL
    `/minds-api-proxy/api/v1/files<absolute_path>`, so a
    grant for `/home/user/foo.txt` matches exactly
    `/minds-api-proxy/api/v1/files/home/user/foo.txt`. The match is a
    regex `pattern`, not a `const`, so the grant also admits the same
    URL with a trailing slash (WebDAV clients commonly emit one when
    treating the target as a collection) and any non-traversing
    sub-path nested below it: a grant on `/home/user/share` therefore
    transitively covers every file and sub-directory inside the
    share. A segment of exactly `..` is rejected anywhere in the
    sub-path (leading, interior, or trailing), so the grant cannot be
    used to escape the shared resource. The legacy
    `queryParams.path` constraint is gone (WebDAV identifies the file
    in the URL path, not via a query parameter).
  - The allowed `method` enum grew from `GET` / `POST` to the full set
    of WebDAV verbs needed to read, write, query, lock, copy, and
    delete the file: `GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`,
    `PROPFIND`, `PROPPATCH`, `MKCOL`, `COPY`, `MOVE`, `LOCK`,
    `UNLOCK`.
  - The wire shape grew an `access` field; the agent now POSTs
    `{type: "file-sharing", payload: {path: "<absolute>", access: "READ" | "WRITE"}}`.
    `access` is required and must be one of the two literal strings
    above (case-sensitive). `READ` unlocks only the non-mutating WebDAV
    verbs (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`); `WRITE` is a strict
    superset that also unlocks the single-path mutating ones (`PUT`,
    `DELETE`, `PROPPATCH`, `MKCOL`, `LOCK`, `UNLOCK`). Per-file
    permission schemas now embed the access mode and the full file
    path in their name (`minds-file-server-read-<absolute-path>` /
    `minds-file-server-write-<absolute-path>`, e.g.
    `minds-file-server-read-/home/user/notes.txt`) so a user can hold
    both grants for the same path independently and a later WRITE
    grant does not silently override an earlier READ grant (or vice
    versa). Re-approving the same `(path, access)` pair remains
    idempotent (same schema name, schemas merge by name on approve).
  - `COPY` and `MOVE` are intentionally **not** in the WRITE verb set.
    Both carry a second path in the WebDAV `Destination` HTTP header,
    and the per-file permission schema only constrains the request
    URL; granting either would let an agent write to any file under
    the WebDAV mount's share roots (`~/` or `/tmp/`) via the
    `Destination` header, regardless of what was actually shared. A
    single-file WRITE grant means "change this one file"; cross-path
    copy/move requires an explicit grant on the destination too.
    Agents that need copy semantics can `GET` the source and `PUT` to
    a destination they have a separate grant for; likewise for move
    (`GET` + `PUT` + `DELETE`).
