# Harden suspicious edge-case handling across `minds env` provisioning

A cleanup pass over `apps/minds/imbue/minds/envs` replacing over-defensive
error handling that could silently mask real failures (leaked cloud resources,
masked data corruption) with handling that surfaces anomalies. User-visible
effects are mostly "a previously-silent failure now raises a clear error" and
better `minds env list` output:

- `minds env destroy` now destroys mngr agents one at a time, so a genuine
  destroy failure on one agent can no longer be masked by another agent
  already being gone (which previously left the failed agent's cloud resources
  stranded while reporting success).
- Tier generation-id reads now surface a malformed/corrupt Vault generation
  entry instead of silently minting a fresh id over it (which would have wiped
  every developer's local state), and use the typed Vault "not found" signal
  instead of matching error text.
- Neon idempotency (already-exists / already-gone) now branches on the HTTP
  status code carried on the error, not a substring of the response body; the
  default branch is resolved via Neon's authoritative `default` flag rather
  than guessing the first-listed branch; and a failed-rollback project delete
  is now logged.
- `modal environment create` idempotency now matches the specific "already
  exists" phrase (the bare "exist" substring also matched "does not exist", so
  a "workspace does not exist" failure was silently treated as success). Modal
  `list --json` parsers now consistently raise on a non-list payload and warn
  on shape-drift rows instead of silently degrading.
- A Cloudflare tunnel identified as ours but with a missing id, an OVH VPS
  whose IAM resource has an empty name, an unreadable-but-present mngr
  `user_id` file, and a broken `$HOME` now raise instead of being silently
  skipped (each previously leaked a resource or hid a broken environment).
- `minds env list` now distinguishes a reserved tier whose committed
  `client.toml` is present but unparseable (`in_repo_malformed`, shown as
  "MALFORMED — fix the committed file") from an unprovisioned env.
- `delete_vault_kv` now detects an already-absent entry by exit code (matching
  `read_vault_kv`) rather than substring-matching the message.
