- The `permission-requests` latchkey gateway extension now expects POST
  bodies with the fields `agent_id`, `key` (string), `value` (list of
  strings), and `rationale` in place of the previous `service_name`
  field. Pending requests are stored under
  `<latchkey-directory>/permission_requests/v1/` so any existing files
  left over from the old shape are silently ignored.
