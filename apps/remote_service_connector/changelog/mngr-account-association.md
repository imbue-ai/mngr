Added workspace-sync storage and endpoints (migration 013): `workspace_records` (per-account plaintext workspace metadata plus an opaque client-encrypted secrets blob, compare-and-swap on a per-row revision, at most one ACTIVE row per agent id) and `account_key_bundles` (the password-wrapped per-account data key).

New admin-authenticated (not paid-gated) routes: `GET /sync/records`, `PUT /sync/records/{host_id}`, `DELETE /sync/records/{host_id}`, `POST /sync/scrub-secrets`, and `GET`/`PUT`/`DELETE /sync/bundle`.
