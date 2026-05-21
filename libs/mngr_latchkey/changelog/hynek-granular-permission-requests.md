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
- ``LatchkeyGatewayClient.get_available_services`` now returns a typed
  ``dict[str, AvailableServiceEntry]`` (pydantic-validated) instead of
  the previous untyped ``dict[str, object]``. Wire-shape validation
  (missing fields, wrong types, empty strings) now happens inside the
  client and surfaces as ``LatchkeyGatewayClientError``.
