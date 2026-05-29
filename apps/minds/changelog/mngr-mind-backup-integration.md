Added a "Backup provider" control to the workspace create form, mirroring the
existing "AI provider" toggle, with three options:

- `imbue_cloud` -- creates a per-workspace R2 bucket (named after the new host
  id) and a scoped key, then injects a `runtime/secrets/restic.env` pointing
  the FCT `host_backup` service at that bucket. Gated on a selected account;
  the default when an account is present.
- `api_key` -- a free-form `KEY=VALUE` block written verbatim to `restic.env`
  (you supply `RESTIC_REPOSITORY` and backend credentials).
- `configure_later` -- injects nothing now; the default when no account is
  selected.

When a real backup provider is chosen, a "Backup encryption method" row
appears: `master_password` (a passphrase established once and saved, mode
0600, to `~/.<minds-env>/backup_password`, shared across all your workspaces;
never re-displayed) or `no_password` (an empty-password repo via restic
`--insecure-no-password`).

Backup setup runs asynchronously after the host is created (mirroring the
Cloudflare tunnel-token injection) and is non-fatal: a failure surfaces as a
notification and leaves the workspace running with backups unconfigured. The
provisioning logic is factored into a single reusable operation
(`configure_backups_for_host`) so it can be re-applied to an existing host
later. Bucket creation is idempotent (an already-created bucket is reused with
a freshly minted key). Destroying a workspace never deletes its bucket or
backups.

New `BackupProvider` / `BackupEncryptionMethod` primitives; new
`mngr imbue_cloud bucket ...` wrappers on the imbue_cloud CLI client.
