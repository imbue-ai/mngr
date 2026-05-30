# Minds Backup Provider Integration

Wire the `imbue_cloud bucket` capability into the minds workspace-creation flow so that a new workspace can automatically provision an off-site restic backup, mirroring the existing "AI provider" toggle pattern.

## Overview

- Add a new **"Backup provider"** toggle to the minds create form, alongside the existing "AI provider" toggle, with three options: `imbue_cloud`, `api_key`, and `configure_later`.
- `imbue_cloud` creates a per-workspace R2 bucket + scoped key (via the `mngr imbue_cloud bucket` capability), then injects restic configuration so the FCT `host_backup` service backs up `/mngr/` to that bucket.
- Backup setup runs **asynchronously after the host is created** (mirroring the existing tunnel-token injection pattern): wait for `mngr create` to return the canonical `host_id`, then create the bucket and inject config via `mngr exec` / `write_file`.
- The injection logic is factored into a single reusable "configure backups for host X" function so it can be re-invoked against any already-created host later (a future management screen, not this PR).
- Backups must work uniformly across docker, lima, and docker-on-VPS hosts (including imbue_cloud) — the snapshot mechanism is already auto-detected by FCT bootstrap, so only the repository address and credentials differ per workspace.
- This PR also adjusts the FCT `host_backup` contract so the repository URL and empty-password behavior are driven by the injected config rather than a hand-edited template.
- `host_backup` has no existing consumers yet, so **backward compatibility is explicitly a non-goal** — the contract is changed cleanly (no shims, no deprecation path) to get the design right.

## Expected Behavior

### Create form

- A "Backup provider" `<select>` appears on the create form (`create.html`), styled and positioned like the "AI provider" select.
  - Options: `imbue_cloud`, `api_key`, `configure_later`.
  - `imbue_cloud` is disabled when no account is selected (same gating as the AI-provider `imbue_cloud` option).
  - Default selection is `imbue_cloud` when an account is selected; otherwise `configure_later`.
- When `imbue_cloud` or `api_key` is selected, a **"Backup encryption method"** row is revealed with two options: `master_password` and `no_password`. Default preselected option is `no_password` (to be revisited later).
  - `configure_later` hides the encryption row entirely.
- `master_password` behavior:
  - If no saved master password file exists yet, the user is prompted to enter a passphrase; on submit it is saved (only when the "save this password" checkbox is checked) to `~/.<minds-env-name>/backup_password` (mode 0600; default env yields `~/.minds/backup_password`). When unchecked, the passphrase is used for this workspace only and not stored.
  - If a saved master password file already exists, the UI simply indicates that a saved password is present. The user does not re-type it, and it is never displayed. minds reads the file only to inject the value into the host.
  - The master password is shared across all of the user's workspaces. Changing it is explicitly out of scope (future feature).
- `no_password` behavior: backups run with restic `--insecure-no-password` (no `RESTIC_PASSWORD`).
- `api_key` behavior:
  - A free-form multiline `KEY=VALUE` textarea is shown, pre-seeded with the common restic env vars as commented-out examples (e.g. `# AWS_ACCESS_KEY_ID=`, `# AWS_SECRET_ACCESS_KEY=`, `# RESTIC_REPOSITORY=`) plus a link to the restic environment-variable docs. `RESTIC_PASSWORD` is intentionally **not** pre-seeded.
  - The textarea contents are written verbatim into `runtime/secrets/restic.env`.
  - Password precedence: if the textarea contains `RESTIC_PASSWORD`, that value is used; otherwise the encryption-method row governs (`master_password` injects the saved/typed passphrase, `no_password` sets the empty-password flag).
  - The user is responsible for providing `RESTIC_REPOSITORY` and any backend credentials they need.

### Workspace creation flow

- The compute provider, AI provider, and backup provider are independent: any combination is valid (e.g. a local docker workspace can still back up to an imbue_cloud bucket if an account is selected).
- After `mngr create` returns the canonical `host_id` and the workspace is ready, backup provisioning runs on a detached thread (it does not block the redirect to the workspace).
- For `imbue_cloud`:
  - A new R2 bucket is created, named exactly the full `host_id` (e.g. `host-<random>`); the server prepends its own account scoping.
  - One `readwrite` bucket key is minted and used for both backup and restore.
  - `runtime/secrets/restic.env` is injected with:
    - `RESTIC_REPOSITORY = s3:<s3_endpoint>/<bucket_name>` (repository at bucket root; no host_id subpath),
    - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from the minted key,
    - `RESTIC_PASSWORD` (from the master password) unless `no_password` was chosen.
  - When `no_password` is chosen, `runtime/backup.toml` is merged to set `restic.allow_empty_password = true`.
- For `api_key`: the textarea is written verbatim to `restic.env`; if `no_password` applies, `backup.toml` is merged to set `restic.allow_empty_password = true`.
- For `configure_later`: nothing is injected. The workspace comes up with backups dormant (FCT bootstrap still seeds the commented-out templates), ready to be configured later via the same reusable function.
- Bucket idempotency: if a bucket named `host_id` already exists (e.g. on a re-run / future configure-later), it is reused and a fresh key is minted rather than erroring.
- Failure handling is minimal: a backup-provisioning failure surfaces as a normal error popup (it occurs after the workspace is already created and usable) and is non-fatal to the workspace. Richer failure UI is future work.

### Backups across host types

- restic backs up `/mngr/` on an hourly cadence to the configured repository on docker, lima, and docker-on-VPS (incl. imbue_cloud) hosts.
- The snapshot method (`direct` / `btrfs_local` / `outer_trigger`) continues to be auto-detected and written into `backup.toml` by FCT bootstrap; this PR does not change that detection.
- Destroying a workspace never deletes its bucket or backups (recovery-first); off-site backups persist for later restore. Bucket cleanup/management is a future, separate feature.

## Changes

### minds app (`apps/minds`)

- `imbue/minds/primitives.py`: add `BackupProvider` (`IMBUE_CLOUD`, `API_KEY`, `CONFIGURE_LATER`) and `BackupEncryptionMethod` (`MASTER_PASSWORD`, `NO_PASSWORD`) enums, following the existing `AIProvider` enum.
- `imbue/minds/desktop_client/imbue_cloud_cli.py`: add bucket wrapper methods (e.g. `create_bucket`, and any list/info needed for idempotent reuse) that shell out to `mngr imbue_cloud bucket ...` and parse the returned bucket info + key material (bucket name, S3 endpoint, access key id, secret access key).
- New module for backup configuration (the reusable "configure backups for host X" operation): given a host/agent id, the chosen backup provider, encryption method, and inputs, it creates/reuses the bucket (imbue_cloud), assembles the `restic.env` contents and any `backup.toml` merge, and injects them into the host via `mngr exec` / `write_file` (mirroring `tunnel_token_injection.py`). Internal only — no CLI/API surface this PR.
- Master-password file helper: resolve the env-scoped path (`~/.<minds-env-name>/backup_password`), read it (for injection) and write it (first-time save, mode 0600), and report whether a saved password exists (for the UI).
- `imbue/minds/desktop_client/agent_creator.py`: thread `backup_provider`, `backup_encryption_method`, and the `api_key` textarea / master-password inputs through `start_creation()` and the background creation flow; after the workspace is ready, kick off the detached backup-provisioning thread that calls the reusable configure-backups function.
- `imbue/minds/desktop_client/app.py`: parse the new fields in the create-form handler and the `/api/create-agent` endpoint; validate provider/account/encryption combinations; surface backup-provisioning failures as an error popup.
- `imbue/minds/desktop_client/templates/create.html` (+ associated JS): add the "Backup provider" select, the conditionally-revealed "Backup encryption method" row, the master-password input / "saved password exists" indicator / "save this password" checkbox, and the `api_key` textarea; wire up show/hide and account-gating logic mirroring the AI-provider controls.
- `imbue/minds/desktop_client/templates.py`: expose the new enum option lists to the create-form template (as is done for AI providers).

### forever-claude-template `host_backup` (`libs/host_backup`)

- Make `RESTIC_REPOSITORY` (from `restic.env`) the sole source of the repository address; drop `repository_url_template` / `template_values` (and the runtime `host_id` URL formatting) from the `backup.toml` contract. No backward-compatibility shim is needed — there are no existing consumers, so old-style `backup.toml` files do not need to keep working.
- Add a `restic.allow_empty_password` setting in `backup.toml` that maps to passing restic `--insecure-no-password` on all restic invocations (init, probe, backup, forget, prune).
- Relax the required-key gating: a workspace is ready to back up when `RESTIC_REPOSITORY` is set and either `RESTIC_PASSWORD` is set or `allow_empty_password` is true. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are no longer strictly required (so non-S3 backends configured via `api_key` work).
- Update FCT bootstrap seeding/templates and any docs (`libs/host_backup/README.md`, default template writers in `config.py`) to match the new contract (repo URL in `restic.env`, empty-password flag in `backup.toml`).

### Tests & changelog

- Unit tests for: the new enums; the master-password file helper (read/write/exists, permissions); `restic.env` / `backup.toml` assembly for each provider + encryption-method combination (including textarea-password precedence and the empty-password flag); the `imbue_cloud_cli` bucket wrappers (parsing + idempotent reuse); `host_backup` config loading with `RESTIC_REPOSITORY` and `allow_empty_password`, and the relaxed gating.
- Integration/acceptance coverage for the end-to-end create-with-backup flow as appropriate per the testing conventions.
- Changelog entries for each touched project (`apps/minds`, and the FCT `host_backup` / `dev` as applicable per that repo's conventions).
