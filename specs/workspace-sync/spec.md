# Workspace Sync: end-to-end-encrypted cross-device sync of workspace metadata and secrets

Status: **Implemented** (branch `mngr/account-association`). Written as a blueprint plan; kept as the design record.

## Overview

- Replace minds' machine-local account/workspace state (`workspace_associations.json`, `backup_password_hash`, `backup_password`) with per-account **workspace records** stored in the `remote_service_connector`, so every device signed into an account sees the same workspace inventory.
- Split each record into **plaintext metadata** (name, color, provider, location, state — readable by any signed-in device without a password) and **`encrypted_secrets`** (the workspace's existing SSH private key + known_hosts + canonical `restic.env`), AEAD-encrypted under a per-account random **DEK**.
- **No key derivation**: existing key material syncs verbatim. The DEK's only job is encrypting `encrypted_secrets`. No `authorized_keys` changes, no mngr-core changes, no restic re-keying.
- The DEK lives raw in a local 0600 file; a copy wrapped by `argon2id(master password)` (the **AccountKeyBundle**) is pushed to the connector **only when the password is non-empty**. Empty password = metadata-only sync tier (always allowable).
- The master password's per-workspace roles are removed: repos are `restic init`'d directly with the workspace's random password (single-key repos), the create form's "backup encryption method" row disappears, and password change becomes rewrap-only (the repo-walking rotation is deleted).
- Migration is a single idempotent step per existing workspace — push its record with secrets; record existence = migrated. Legacy files are converted once and renamed aside (`.pre-sync`); no fallback read paths.
- Association = record existence under an account. Cross-account moves are disassociate-then-associate (re-encrypt secrets under the other DEK; no rekeying). Leased imbue_cloud workspaces get records like everything else, with association immutable (lease account).
- Sync is a **free** feature (no paid gate). Backup downloads from other devices use the synced credentials directly, so the paid gate (which only guards key *minting*) is not involved.
- Compromise recovery ships as schema (`key_epoch`) plus a design section only; no rotation tooling.

## Expected behavior

### Steady state, one device (today's user)
- Everything works as before; additionally each associated workspace's record (metadata + secrets) is kept up to date on the connector via write-through pushes.
- Renaming/recoloring while offline applies locally and is pushed by the reconcile when connectivity returns (dirty-flagged replica rows). Associate/disassociate require the connector to be reachable and fail cleanly offline.
- Private (unassociated) workspaces have **no record**; nothing about them ever leaves the machine.
- Setting up backups no longer asks for an encryption method; the repo is created with the workspace's random password only.

### Master password
- Purely the wrapping secret for the DEK. Setting/changing it (settings page, same surface as today): verify by unwrapping, rewrap, push the bundle; on the empty→non-empty transition, also push all pending `encrypted_secrets`.
- Clearing it (non-empty → empty): full server scrub — bundle deleted, `encrypted_secrets` stripped from all the account's records; metadata stays; everything re-pushes if a password is set again.
- With multiple signed-in accounts, one typed password is tried against every account's bundle; the UI communicates which accounts remain locked (they may need an older password).

### New device
- Sign in → records pull → all workspaces render immediately (no password needed for metadata).
- Workspaces hosted elsewhere render greyed-out with the provider badge repurposed as a **location badge** ("on \<device\>"); they are not clickable; backup download/status links work once unlocked.
- A persistent subtle banner prompts for the master password; unlock is lazy (first secrets-needing operation also prompts). Unlock fetches the bundle, unwraps the DEK, and writes the local DEK file.
- Leased imbue_cloud workspaces render as normal live workspaces on every signed-in device (discovery already finds them; records supply the SSH key + backup env).

### Destroy and staleness
- Destroy through minds tombstones the record (`state=DESTROYED`) but keeps metadata + secrets, so a destroyed workspace's backups stay downloadable from any device. DESTROYED records are hidden in the v1 UI.
- Out-of-band destroys (e.g. `mngr destroy` from the CLI): the owning device's reconcile tombstones a record only when the host is definitively absent from its local providers (successful enumeration, host gone — not a failed poll). Greyed-out entries also get a manual "remove from list" action as an escape hatch.

### Migration (existing installs)
- After the session's first complete discovery snapshot, the reconcile runs: every associated workspace without a record gets one pushed (metadata + secrets); `workspace_associations.json` is converted then renamed to `.pre-sync`; the password hash+plaintext files are verified, folded into the DEK bundle, and renamed likewise.
- Existing repos keep their extra legacy keys (old master-password keys rot harmlessly); workspace-injected `restic.env` files are never touched.

## Implementation plan

### `apps/remote_service_connector`
- `migrations/013_workspace_sync.sql`:
  - `workspace_records`: `user_id`, `host_id` (PK pair), `agent_id`, `display_name`, `color` (nullable), `provider_kind`, `hosting_device_id` (nullable; null for cloud rows), `device_label`, `state` (`active`/`destroyed`), `restored_from_host_id` (nullable), `backup_kind` (`imbue_r2`/`api_key`/`none`), `encrypted_secrets` (BYTEA, nullable, size-capped), `revision` (int), `created_at`, `updated_at`. Partial unique index on `(user_id, agent_id) WHERE state = 'active'`.
  - `account_key_bundles`: `user_id` (PK), `kdf_salt`, `kdf_time_cost`, `kdf_memory_kib`, `kdf_parallelism`, `wrapped_dek`, `key_epoch`, `updated_at`.
- New endpoints in `app.py` (SuperTokens admin auth via `authenticate_request`/`require_admin`; **not** paid-gated; `handle_endpoint_errors` wrapping):
  - `GET /sync/records` — all of the caller's records.
  - `PUT /sync/records/{host_id}` — upsert; CAS on `revision` (409 on mismatch, echoing the stored row).
  - `DELETE /sync/records/{host_id}` — remove the row outright (disassociation / remove-from-list).
  - `POST /sync/scrub-secrets` — strip `encrypted_secrets` from all caller's records (clear-password flow).
  - `GET|PUT|DELETE /sync/bundle` — AccountKeyBundle fetch/replace/delete.
- Request/response pydantic models mirroring the row shapes; `encrypted_secrets` transported base64; server-side caps (secrets size, name lengths).

### `libs/imbue_common`
- New `imbue/imbue_common/secret_wrapping.py` (pure, no I/O):
  - `derive_kek(password, salt, params) -> bytes` (argon2id via `argon2-cffi` low-level API).
  - `wrap_dek(kek, dek) -> bytes` / `unwrap_dek(kek, wrapped) -> bytes` (AESGCM from `cryptography`; raises typed `WrongPasswordOrCorruptDataError` on tag failure).
  - `encrypt_secrets(dek, plaintext_bytes) -> bytes` / `decrypt_secrets(dek, blob) -> bytes`.
  - Constants: KDF defaults, nonce sizes; `generate_dek()`.

### `libs/mngr_imbue_cloud`
- `connector/client.py`: `list_sync_records`, `put_sync_record`, `delete_sync_record`, `scrub_sync_secrets`, `get_key_bundle`, `put_key_bundle`, `delete_key_bundle` (Bearer JWT, transparent refresh as existing calls).
- `data_types.py`: `SyncWorkspaceRecord`, `SyncKeyBundle` wire models (plaintext fields + base64 secrets; no crypto here).
- `cli/sync.py`: new `sync` click group registered in `cli/root.py` — `records pull`, `records push` (record JSON on stdin), `records delete`, `scrub-secrets`, `bundle pull|push|delete`; `--account` option; JSON-on-stdout/stderr contract from `cli/_common.py`. Pure transport: the plugin never sees plaintext secrets or the DEK.

### `apps/minds`
- New `desktop_client/dek_store.py`: per-account DEK files at `~/.minds/keys/<user_id>.dek` (0600, atomic writes); create-if-absent; lock status (`is_unlocked(user_id)`); unlock (fetch bundle via CLI → unwrap → write file); wrap-and-push; per-account results for the "which accounts are still locked" surface.
- New `desktop_client/workspace_record_store.py`: the local replica + sync engine —
  - replica persistence (`~/.minds/workspace_records/<user_id>.json`, dirty flags, last-pulled snapshot);
  - record assembly: metadata from the resolver (name, color, provider, host_id, agent_id) + `device_id` (minds env's mngr `host_id`) + `device_label` (hostname);
  - secrets assembly per provider: the SSH private key that grants access (per-host key when the provider has one, e.g. imbue_cloud's `hosts/<host_id>/ssh_key`; else the provider-wide key), its known_hosts entries, and the canonical `restic.env` text when present;
  - push (CAS retry loop), pull, merge, and the post-discovery reconcile (migrate unmigrated, push dirty, tombstone definitively-absent, one-shot legacy conversion with `.pre-sync` renames).
- `desktop_client/imbue_cloud_cli.py`: typed wrappers for the new `sync` verbs.
- `desktop_client/session_store.py`: `MultiAccountSessionStore` reworked — associations are now record existence in the replica; `associate_workspace`/`disassociate_workspace` become record create / delete operations that require connectivity (tombstoning is reserved for destroyed workspaces); identity caching unchanged; orphan-GC machinery removed with the legacy file.
- `desktop_client/backup_password_store.py`: reduced to password verification-by-unwrap + `is_master_password_set` (bundle/dek presence); `backup_password_rotation.py` deleted outright.
- `desktop_client/backup_provisioning.py`: `BackupSetupRequest.master_password` removed; `restic init` with the workspace password directly; `write_canonical_env`/`archive_canonical_env` call sites trigger a record secrets re-push.
- `desktop_client/backup_status.py` / `backup_export.py`: env resolution falls back to materializing `~/.minds/backup_envs/<agent_id>.env` from the record's decrypted secrets when no local file exists (other-device and post-restore cases).
- `desktop_client/app.py`: `_build_workspace_list` merges replica rows (local discovery wins by host_id; remaining rows render greyed with `location` + `device_label` fields); `_handle_backup_password_change` swapped to rewrap/scrub mechanics with per-account lock reporting; unlock prompt endpoint + banner state; "remove from list" endpoint; reconcile scheduling hooked to the first complete discovery snapshot; `ensure_backup_password_hash` startup hook replaced by DEK/legacy-conversion bootstrapping.
- `desktop_client/workspace_create.py` / `agent_creator.py`: on-created callback pushes the record (with secrets when backups provision); create-form template drops the encryption-method row.
- `desktop_client/destroying.py`: destroy flow tombstones the record.
- Chrome templates/static JS: location badge, greyed state, unlock banner, remove-from-list action.

## Implementation phases

1. **Foundations** — `imbue_common.secret_wrapping` (+ tests); connector migration + `/sync/*` endpoints; plugin client methods + `sync` CLI verbs. Fully testable without minds.
2. **Key lifecycle in minds** — `dek_store`, settings-page password mechanics swap (rotation module deleted), provisioning simplification (single-key repos), legacy password-file conversion. Minds works exactly as before for a single device, now DEK-backed.
3. **Records** — `workspace_record_store`, write-through at all mutation sites, post-discovery reconcile + associations migration, `session_store` rework. Records become the source of truth; server inventory complete.
4. **UI + cross-device flows** — merged workspace list with location badges and greyed entries, unlock banner + prompt, other-device backup status/export via materialized envs, remove-from-list.
5. **Polish** — acceptance test, docs (`apps/minds/docs/`), changelog entries per touched project, ratchet updates.

## Testing strategy

- **Unit**: crypto round-trips (wrong password, tampered blob, empty password); wire-model serialization; replica dirty/merge/CAS-retry logic against a mock `ImbueCloudCli`; secrets assembly per provider layout; legacy-file conversion (including `.pre-sync` renames and idempotent re-run); association semantics (one ACTIVE per agent_id, leased-host 403 retained); password set/change/clear state transitions.
- **Integration**: connector `/sync/*` endpoints via the existing in-process test pattern (CAS conflicts, scrub, bundle lifecycle, size caps, cross-user isolation — user A cannot read/write user B's rows); minds reconcile end-to-end against a mocked connector (migrate → push → pull → merge → tombstone).
- **Acceptance** (dev-tier connector, `@pytest.mark.acceptance`): sign in, provision a workspace record + bundle from data dir A; from a fresh data dir B: sign in, pull, verify metadata renders without a password, unlock with the master password, decrypt secrets, materialize the backup env, and read backup status.
- **Edge cases**: empty-password tier (records without secrets; banner behavior); clear-password scrub; multi-account partial unlock reporting; offline rename queueing; associate offline (clean failure); out-of-band destroy tombstoning (definitively-absent rule vs failed poll); destroyed-workspace backup download.
- No e2e/`deployment_tests` changes.

## Open questions

- Exact argon2id parameters (proposal: RFC 9106 low-memory profile) and the `encrypted_secrets` size cap (proposal: 256 KiB).
- Replica granularity: one JSON file per account vs per-record files (proposal: per-account file; records are small).
- Precise "definitively absent" rule for tombstoning (proposal: the record's provider enumerated successfully in this session AND the host is past the provider's destroyed-host persistence window).
- Whether `mngr imbue_cloud sync` CLI verbs are marked experimental/hidden in help output (they exist for minds, not humans).
- What happens to the local replica for an account that signs out (proposal: keep the file; it's harmless and avoids re-pulling on re-signin — but secrets stay encrypted and the DEK file could optionally be deleted on signout).
- Whether the periodic pull tick piggybacks on the chrome SSE loop or gets its own timer (proposal: own timer, ~60s, paused when no chrome is connected).
- Compromise-recovery design section detail: on `key_epoch` bump, all secrets re-encrypt + re-push and the bundle rewraps; underlying key material (SSH keys, restic passwords, R2 keys) rotation remains a documented manual procedure for now.
