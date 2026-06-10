Every per-agent latchkey permissions baseline now materializes -- but does not pre-grant -- a `minds` detent scope with three named permissions (`minds-create`, `minds-status`, `minds-logs`) gating the minds desktop client's peer-mind management endpoints reached through the gateway's `minds-api-proxy` extension (`POST /minds-api-proxy/api/create-agent` and the per-creation `/status` and `/logs` paths).

A "Peer minds" entry is added to the latchkey services catalog (`extensions/services.json`) so the desktop client's permission dialog can offer the three named permissions.

A new idempotent migration, `ensure_minds_schema_in_existing_host_files`, injects the `minds` scope and named-permission schemas into pre-existing per-host `latchkey_permissions.json` files (called by `minds run` at startup, before the gateway restart).

A `dev` optional-dependency extra adds `jsonschema`, used by unit tests to validate the new detent schemas against a representative request matrix.
