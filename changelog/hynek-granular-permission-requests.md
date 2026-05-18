- The `permission-requests` latchkey gateway extension now expects POST
  bodies with the fields `agent_id`, `scope` (string), `permissions`
  (list of strings), and `rationale` in place of the previous
  `service_name` field. Pending requests are stored under
  `<latchkey-directory>/permission_requests/v1/` so any existing files
  left over from the old shape are silently ignored.
- The `permissions` latchkey gateway extension now exposes
  `GET /permissions/available`, returning the catalog of services the
  gateway knows how to grant permissions for as a JSON array of
  `{"scope": {"schema_name", "display_name"}, "permissions": [...]}`
  objects. The catalog is backed by a `services.json` data file that
  ships alongside the extensions and is materialized into
  `LATCHKEY_DIRECTORY/extensions/` together with the `.mjs` files at
  gateway-spawn time.
