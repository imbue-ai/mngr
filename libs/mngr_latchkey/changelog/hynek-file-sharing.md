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
