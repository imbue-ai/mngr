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
