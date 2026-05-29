Added a "Backup provider" control to the workspace create form, mirroring the
existing "AI provider" toggle, with three options:

- `imbue_cloud` -- creates a per-workspace R2 bucket (named after the new host
  id) and a scoped key, then injects a `runtime/secrets/restic.env` pointing
  the FCT `host_backup` service at that bucket. Gated on a selected account;
  the default when an account is present.
- `manual` -- a free-form `KEY=VALUE` block written verbatim to `restic.env`
  (you supply `RESTIC_REPOSITORY` and backend credentials).
- `configure_later` -- injects nothing now; the default when no account is
  selected.

When a real backup provider is chosen, a "Backup encryption method" row
appears: `master_password` or `no_password`. The conditional backup fields
(restic environment, encryption method, master password) render as standard
label-on-left / field-on-right rows like the rest of the form.

minds (which now requires `restic` to be installed on the machine running it)
initializes each workspace's restic repository itself and gives the workspace
its own random repository password, so the master password never enters the
workspace. Enabling backups: resolve the repository + credentials, generate a
random per-workspace password, `restic init` the repo with the master
password (or empty for `no_password`), `restic key add` the random password,
write the canonical `restic.env` to a 0600 minds-side file, and inject that
file into the workspace. The `api_key` block must not set `RESTIC_PASSWORD`
(minds assigns it).

Backup setup runs asynchronously after the host is created (mirroring the
Cloudflare tunnel-token injection) and is non-fatal: a failure surfaces as a
notification and leaves the workspace running. The reusable
`configure_backups_for_host` operation can be re-applied to an existing host
later and is idempotent (an existing canonical env is re-injected; an
already-created bucket / initialized repo is reused). The canonical
`restic.env` is never auto-deleted, so a stopped or destroyed workspace's
backups stay recoverable.

The Projects page now shows each project's backup status (Backing up / Backed
up N ago / No backups / Unknown), fetched once on load from a new
`/api/backup-status` route that queries restic per project from the minds
machine.

New `BackupProvider` / `BackupEncryptionMethod` primitives; new
`mngr imbue_cloud bucket ...` wrappers on the imbue_cloud CLI client.
