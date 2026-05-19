- The `permission-requests` latchkey gateway extension now expects POST
  bodies with the fields `agent_id`, `scope` (string), `permissions`
  (list of strings), and `rationale` in place of the previous
  `service_name` field. Pending requests are stored under
  `<latchkey-directory>/permission_requests/v1/` so any existing files
  left over from the old shape are silently ignored.
- The `permissions` latchkey gateway extension now exposes two new
  catalog endpoints: `GET /permissions/available` returns the full
  catalog as a JSON object keyed by raw service name, and
  `GET /permissions/available/<service_name>` returns a single entry
  (or 404 if the service is unknown). Each catalog value has the
  shape `{"scope": "<schema_name>", "display_name": "...",
  "permissions": [...]}`. The catalog is backed by a `services.json`
  data file that ships alongside the extensions and is materialized
  into `LATCHKEY_DIRECTORY/extensions/` together with the `.mjs` files
  at gateway-spawn time.
- The default permissions seeded for every new agent are broadened to
  let the agent read its own current permissions
  (`GET /permissions/self`) and read the per-service catalog entry
  (`GET /permissions/available/<service_name>`) in addition to the
  existing ability to file a new permission request
  (`POST /permission-requests`). The catalog read is granted under a
  path-pattern Detent permission schema (matching
  `/permissions/available/<service_name>` only) so the agent baseline
  does not also expose the unbounded collection endpoint.
- The minds desktop client has been adapted to the new request shape:
  `LatchkeyPermissionRequestEvent` now carries `scope` (Detent schema)
  and `permissions` (the agent's requested list) instead of
  `service_name`. The previously-bundled
  `apps/minds/imbue/minds/desktop_client/latchkey/services.toml` has
  been deleted; the desktop client now lazily fetches the catalog from
  the gateway's `/permissions/available` endpoint (cached in process)
  to look up display names and the legal permission set. The grant
  dialog continues to render the display name ("Slack" etc.) and lets
  the user broaden or narrow the requested permission set.
- Fixed a startup race where the minds desktop client could cache a
  stale latchkey gateway port and then fail every subsequent call
  with ``[Errno 111] Connection refused``. The race occurred because
  the supervisor restart and the gateway-client pre-warm previously
  ran on independent background threads at minds startup: the
  gateway client could observe the previous supervisor's record
  (still on disk, still alive) before the restart deleted that
  record and stamped the fresh port. Two fixes:
  - ``LatchkeyGatewayClient`` now self-heals from a stale cached
    gateway URL on connect-level transport failures
    (``httpx.ConnectError`` / ``httpx.ConnectTimeout``): the cached
    URL is invalidated and the next call re-resolves the port from
    the supervisor's on-disk record. Non-connect errors (read
    failures mid-stream, 5xx responses, etc.) continue to propagate
    without invalidation, since those usually indicate a problem at
    the gateway end rather than a stale local cache.
  - The supervisor restart and the gateway-client pre-warm now run
    sequentially on a single background thread, eliminating the
    race in the first place. App startup is unaffected: this still
    runs in a background thread, so the supervisor restart's 10s
    SIGTERM grace never blocks the foreground startup path.
- ``LatchkeyGatewayClient.get_available_services`` now returns a typed
  ``dict[str, AvailableServiceEntry]`` (pydantic-validated) instead of
  the previous untyped ``dict[str, object]``. Wire-shape validation
  (missing fields, wrong types, empty strings) now happens inside the
  client and surfaces as ``LatchkeyGatewayClientError``; the catalog
  module that consumes the result has been simplified accordingly
  and no longer maintains its own parallel ``_RawServiceEntry`` /
  ``MalformedServicesCatalogError`` validation layer.
- The latchkey permission dialog no longer pre-checks the catch-all
  ``any`` permission as an implicit default. ``any`` is still offered
  as the first checkbox so the user can opt into unrestricted access
  explicitly, but the initial check state is now the union of (a)
  permissions already granted for the scope on the agent's host and
  (b) the permissions the agent declared in the request event.
  Approving without modification therefore grants exactly that union
  (matching the user's mental model of "give the agent what it's
  asking for, on top of what it already has"). Previously, existing
  grants alone seeded the pre-check and the agent's new ask was
  ignored unless the user actively ticked it; under the new behavior
  an unmodified Approve actually delivers the requested permissions.
