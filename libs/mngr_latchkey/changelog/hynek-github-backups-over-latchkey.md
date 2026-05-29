Bump Latchkey to version 2.14.0 to support GitHub git operations via Latchkey gateway.

Changed the `services.json` catalog (and the `permissions` gateway extension that reads it) so each raw service name now maps to a *list* of scope entries instead of a single entry. This lets one service expose more than one detent scope. The `GET /permissions/available` and `GET /permissions/available/<service_name>` endpoints now return arrays of `{scope, display_name, permissions}` objects per service.
