# Minds-Managed Backup Keys + Backup Status

Rework minds backup provisioning so the master password never enters the workspace, the restic repo is initialized from the minds app (not the workspace), and per-project backup status is shown on the landing page. Builds on the existing "Backup provider" feature; no backwards compatibility is required (nothing uses this yet).

## Overview

- minds initializes the restic repo itself and gives each workspace its own random repository password, so the workspace never holds the user's master password and carries no repo-init logic.
- restic is required on the machine running the minds app (assume it is on `PATH`; bundling it is out of scope; fail with a clear error if it is missing).
- The minds-side copy of a workspace's `restic.env` becomes the **canonical** definitive copy; the copy inside the workspace is just an injected mirror of it. Config changes are made to the canonical copy and re-injected whole.
- Because minds can now run restic against each repo from outside the workspace, the landing page shows each project's backup status (currently backing up, or how long since the last successful backup).
- The FCT `host_backup` service is simplified: it no longer initializes the repo and no longer needs the empty-password path.

## Expected Behavior

### Provisioning (applies to both `imbue_cloud` and `api_key`)

- The user can never set `RESTIC_PASSWORD`. The `api_key` free-form textarea rejects a submission that defines `RESTIC_PASSWORD`, with an inline error explaining that minds assigns each workspace its own random repository password.
- When backups are enabled (in the existing async post-creation provisioning thread), minds:
  1. Resolves the repository URL + backend credentials — `imbue_cloud`: create/reuse the per-workspace R2 bucket + readwrite key; `api_key`: from the textarea (`RESTIC_REPOSITORY` + backend creds).
  2. Generates a random per-workspace `RESTIC_PASSWORD`.
  3. Runs `restic init` from the minds machine against the repo, authenticating with the user's master password — or an empty password when the encryption method is `no_password` (the empty recovery key is accepted for now; access to the bucket is the real gate).
  4. Runs `restic key add` to add the random per-workspace password as an additional repo key.
  5. Writes the canonical per-workspace `restic.env` (repository URL + backend creds + the random password) to a 0600 file under the minds data dir.
  6. Injects that whole file into the workspace at `runtime/secrets/restic.env` via `mngr exec`.
- The encryption-method row (`master_password` / `no_password`) now governs only the master/recovery key the repo is initialized with. The workspace always receives the random key, never the master password.
- The workspace never initializes the repo and never sees the master password.
- Failure handling is unchanged in spirit: restic missing, or `init` / `key add` failing (e.g. network), is non-fatal — the workspace is still created and the failure surfaces via the existing error popup. The project's backup status then reads "Unknown" / "No backups".
- Re-provisioning is idempotent: if a canonical env file already exists for the workspace, minds reuses it and just re-injects it (skips `init` / key generation). If the canonical file is missing but the repo already exists (e.g. a reused bucket), `restic init`'s "already initialized" outcome is treated as success.
- The per-user master-password flow is otherwise unchanged (established once, saved 0600, shared across the user's workspaces); only its use moves to `restic init` time on the minds machine.

### Canonical env file lifecycle

- The minds-side canonical `restic.env` is the source of truth for how to reach a workspace's backups (it holds the random key that opens the repo).
- It is never auto-deleted, including on workspace destroy, so backups stay recoverable for an offline or destroyed workspace. (Destroying a workspace already never deletes the bucket/backups.)

### Backup status on the landing page

- A new route (e.g. `GET /api/backup-status`) returns `{agent_id: {state, last_success_at}}` for all currently known projects.
- The server computes status in parallel, one restic invocation set per project, with a per-project timeout, using that project's canonical env file and `restic --no-lock` (so status checks never create locks):
  - last successful backup time via `restic snapshots --latest 1` (JSON),
  - in-progress via `restic list locks`, where only a non-stale lock (younger than restic's staleness window) counts as "backing up".
- The landing page JS fetches this route once on page load and fills each project tile with one of: "Backing up…", "Backed up <N> ago", "No backups" (no canonical env / backups not configured), or "Unknown" (restic error/timeout).

### FCT `host_backup` simplification

- `host_backup` no longer probes-then-initializes the repo; it assumes minds already initialized it.
- The `allow_empty_password` setting and `--insecure-no-password` handling are removed entirely — the workspace always has a real random `RESTIC_PASSWORD`. (Reverts that part of the prior contract; nobody depends on it.)

## Changes

### minds app (`apps/minds`)

- New minds-side restic wrapper (analogous to the FCT `host_backup` restic helpers) that shells out to local `restic`: `init`, `key add`, `snapshots --latest 1 --json --no-lock`, `list locks --json --no-lock`. Detects a missing `restic` binary and raises a clear error.
- Random per-workspace password generation (cryptographically secure).
- `backup_provisioning.py`: replace "render restic.env (with master/empty password) + flip `backup.toml` allow_empty_password + let the workspace init" with the new flow — resolve repo+creds, generate random key, `restic init` (master/empty) + `restic key add` (random), write the canonical env file, inject it. Drop the `allow_empty_password` / `backup.toml` merge path. Make re-provisioning reuse an existing canonical env.
- Canonical per-workspace `restic.env` store: a 0600 file per workspace under the minds data dir (read by the status route; never auto-deleted, incl. on destroy).
- `api_key` validation: reject a textarea that defines `RESTIC_PASSWORD` (form handler + API endpoint), with an explanatory message; reflected on the create form.
- New backup-status route + a small status model (`{state, last_success_at}`), computed in parallel with per-project timeouts.
- `templates.py` / `landing.html` (+ landing JS): accept a `backup_status_by_agent_id` map and render per-tile status; fetch `/api/backup-status` once on load and populate tiles. (Mirrors the existing per-agent dict pattern like `agent_names` / telegram status.)
- Master-password store usage: consumed at init time only; no longer injected into the workspace. (Module otherwise unchanged.)
- Remove the now-unused pieces of the previous approach (workspace-injected master password, `backup.toml` empty-password merge).

### forever-claude-template `host_backup` (separate repo / worktree)

- Remove repo probe-then-init logic; the service assumes the repo exists.
- Remove `allow_empty_password` from `backup.toml` and `--insecure-no-password` from all restic invocations; restore the gating to require `RESTIC_REPOSITORY` + `RESTIC_PASSWORD`.
- Update default templates, README, and tests to match.

### Tests & changelog

- Unit tests: random-key generation; `api_key` textarea `RESTIC_PASSWORD` rejection; canonical env file write/read/permissions and never-deleted-on-destroy; re-provisioning idempotency (reuse existing canonical env; treat already-initialized repo as success); the minds restic wrapper argument construction + output parsing (snapshots/locks → status, including stale-lock handling and the missing-`restic` error); backup-status route shape + per-project error/timeout → "Unknown". Use a real/local restic where feasible, otherwise a canned wrapper per existing convention.
- `host_backup` tests updated for the removed init / empty-password paths.
- Changelog entries for each touched project per repo convention.
