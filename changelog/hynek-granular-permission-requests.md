- The `permission-requests` latchkey gateway extension now expects POST
  bodies with the fields `agent_id`, `scope` (string), `permissions`
  (list of strings), and `rationale` in place of the previous
  `service_name` field. Pending requests are stored under
  `<latchkey-directory>/permission_requests/v1/` so any existing files
  left over from the old shape are silently ignored.
- The `permissions` latchkey gateway extension now exposes
  `GET /permissions/available/<service_name>`, which returns the
  permission catalog entry for `<service_name>` (e.g. `slack`,
  `google-gmail`) as a `{"scope": "<schema_name>", "permissions":
  [...]}` object, or 404 if the service is unknown. The catalog is
  backed by a `services.json` data file (keyed by raw service name)
  that ships alongside the extensions and is materialized into
  `LATCHKEY_DIRECTORY/extensions/` together with the `.mjs` files at
  gateway-spawn time.
