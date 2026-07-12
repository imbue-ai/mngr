# Injected Backup Service: Drift Detection + Idempotent Update

Make the minds backup service (the FCT `host_backup` restic service) something that can be directly, idempotently configured on a *running* workspace -- not just at creation -- and make it possible to tell, from the minds desktop app, whether a workspace's backup service matches what we would set up today. When it doesn't, show a warning badge and offer a one-click idempotent update. The same machinery powers post-creation settings changes (enable backups later, change the backup destination).

Two workstreams, one plan: a refactor of the backup service in the forever-claude-template repo (executed via the `.external_worktrees/` convention, sequenced first), then the minds-side verification/update machinery in this monorepo.

## Overview

- The workspace's backup code is delivered and verified **via git**: the workspace already shares history with the upstream forever-claude-template, and `minds-v*` release tags are synced between the two repos. Updating means checking out `libs/host_backup/**` at the tag matching the running minds app version and committing it; verifying means diffing the workspace's current content against that tag.
- To make the comparison surface clean, the FCT `host_backup` service is refactored so its on-workspace footprint is exactly `libs/host_backup/**` plus per-workspace data that minds owns (`runtime/secrets/restic.env`): snapshot settings become in-memory "backup capabilities" detected by the service itself (not config, not compared), `backup.toml` becomes purely optional user settings (ignored by comparison, tolerant of bad input), and bootstrap gets out of the backup business entirely.
- The drift check runs as an expanded part of the existing backup status batch: for each online, verification-enabled workspace, one `mngr exec` runs a check script that reports code state (vs the target tag), service state (supervisord), and env state (hash vs the minds-side canonical env). Any detected problem -- including "backups not configured at all" -- surfaces as one warning badge on the workspace tile, with a per-check breakdown and a single idempotent "Update backup service" action in the workspace settings view.
- The update runs as a single script on the workspace, tracked as a workspace operation: gate on actively-running chat agents (code path only), wait for any in-flight backup tick, stash / checkout tag / commit `backup-update: minds-v<X>` / `uv sync` / restart via supervisorctl / verify, auto-rollback (`git revert`) on failure.
- The same idempotent machinery covers enabling backups on a `CONFIGURE_LATER` workspace, repairing a drifted/missing `restic.env`, adopting an externally-configured env into the minds canonical store, and changing a workspace's backup destination. Master-password rotation is explicitly deferred (orthogonal; will reuse this machinery).

## Expected Behavior

### FCT host_backup ownership refactor

- The `host_backup` service detects its own "backup capabilities" (the snapshot mechanism) in memory at startup, using the same decision tree bootstrap uses today: `/mngr-snapshot/` directory present -> `outer_trigger`; `findmnt` reports `/mngr` on btrfs -> `btrfs_local`; otherwise -> `direct`. All paths are well-known constants or derived from `host_dir` (confirmed fully in-container-probeable). Capabilities are logged and included in the backup events stream; there is no persisted capabilities file and no override mechanism.
- `runtime/backup.toml` becomes purely optional user settings (interval, retention, excludes). When absent, the service runs on built-in defaults. Unknown keys, a stale `[snapshot]` section (old bootstraps keep writing one forever), or malformed values produce log warnings/`tick_error`-style events -- never a crash and never a refusal to run with the remaining valid settings.
- Bootstrap's backup init is deleted: no default `backup.toml`, no commented-out `restic.env` template. A missing `runtime/secrets/restic.env` simply means "not configured" (the service keeps idling with its existing missing-secrets event). minds is the only writer of `restic.env`.
- `host_backup.config` keeps **no-op backwards-compatibility shims** for every name old bootstrap imports (old workspaces keep their old bootstrap forever; a renamed import would crash boot before supervisord starts). The shims are commented as removable once all pre-refactor hosts have rotated out.
- Stable contract (documented in `libs/host_backup/README.md`): the `[program:host-backup]` supervisord block, the root `pyproject.toml` workspace registration, and the `uv run host-backup` entry point never change via the injection mechanism. Dependency changes are absorbed by regenerating `uv.lock` on the workspace with plain `uv sync` (not `--frozen`).

### Verification (drift detection)

- Runs on every backup status batch, concurrent with the existing laptop-side restic status checks, for each **online** workspace whose verification is enabled. Offline workspaces report "offline" for this part; the UI shows nothing extra for them.
- A per-workspace "disable/enable backup verification" toggle (workspace settings, default enabled, stored minds-side) suppresses both the checks and the badge entirely when disabled. Snapshot status ("Backed up N ago") keeps working and displaying regardless -- it never needed the workspace online.
- The check is one `mngr exec` running a minds-rendered script that reports structured JSON:
  - **Code state**: ensures the `upstream` remote exists (from `parent.toml`), fetches tags if the target tag is missing locally (full tag fetch, matching `update-self` behavior), then compares `libs/host_backup/**` content against the tag `minds-v<app version>`. Matches -> up to date. Differs but the tag is an ancestor of HEAD -> **newer, not flagged** (also silently accepts user edits on top). Differs otherwise -> outdated.
  - **Service state**: the `host-backup` program is known to supervisord and RUNNING.
  - **Env state**: content hash of `runtime/secrets/restic.env`, which minds compares against its canonical env.
- **Adoption**: if minds holds no canonical env but the workspace has a `restic.env` that parses with `RESTIC_REPOSITORY` + `RESTIC_PASSWORD`, the check automatically pulls it into the canonical store; status and management just start working. (This also covers a second minds install managing the same workspace.)
- Problem states, all sharing one warning-badge style with a distinguishing tooltip: **not configured** (going without backups is an error state to resolve), **code outdated**, **env mismatch or missing on the workspace**, **service not running**, and **unverifiable** (an online workspace where the check itself failed: no upstream remote, fetch failure, script error). The settings view shows the full per-check breakdown, including installed-vs-desired version strings (nearest `backup-update:`/tag identity vs `minds-v<X>`).
- The first release with this feature intentionally flags every pre-existing workspace (they are all behind the new tag by construction); the badge drives the fleet to converge.

### The update operation

- One idempotent "Update backup service" action converges everything the check found: code to the target tag, env re-injected from the canonical store, service restarted/registered. There is a single fix button; the breakdown is informational.
- The update runs as a **single script** pushed to the workspace via `mngr exec`, tracked as a workspace operation (the `workspace_operations` pattern used by restart) with step-level status the settings view can show. One tracked operation at a time per workspace (serialized with restart and each other); concurrent updates across different workspaces are fine.
- Script sequence for the code path:
  1. Gate: error out if any **chat agents** are actively RUNNING (the system-services agent and worker agents never count). The error surfaces in the UI with a convenient "Stop all chats and retry" action that stops the chats and automatically re-runs the update; chats stay stopped afterward (they resume on the user's next message anyway).
  2. Wait for any in-flight backup tick to finish -- indefinitely, as a visible, cancellable step of the tracked operation ("waiting for in-progress backup to finish").
  3. `git stash` any uncommitted changes; ensure `upstream` remote; fetch tags; `git checkout minds-v<X> -- libs/host_backup`; commit with subject `backup-update: minds-v<X>` (documented alongside the `update-self:` convention so built-in-code classifiers can match it).
  4. `uv sync` (regenerates `uv.lock` as needed), `supervisorctl restart host-backup`, verify the program reaches RUNNING.
  5. `git stash pop`. On pop conflict: leave the changes in the stash and report success-with-warning pointing the user at their stashed changes.
- On failure (e.g. `uv sync` fails, service does not come back RUNNING): **auto-rollback** -- `git revert` the update commit (a new commit; never amend/rebase), re-run `uv sync`, restart the old service, unstash, and report failure with details. History keeps both commits.
- Env-only convergence (re-inject, enable, change destination) does not touch the repo and runs anytime -- the chat gate applies only to the code path. Before writing a changed env, the existing workspace `restic.env` is rotated to `restic.env.<date>`.

### Settings flows (workspace settings screen)

- **Enable backups** on a `CONFIGURE_LATER` workspace: the same provider/encryption inputs as the create form (imbue_cloud / api_key; master password / no password), driving the existing idempotent `configure_backups_for_host` path.
- **Change destination**: switching provider or pointing api_key at a new repo (imbue_cloud -> imbue_cloud is not a change -- same R2 bucket). Implemented as idempotent fresh provisioning against the new inputs: new random per-workspace password, `restic init` with the master password, `restic key add`. The old canonical env is archived minds-side (mirroring the workspace-side `.date` rotation) and the workspace env rotated + re-injected. Existing snapshots stay in the old repo, reachable via the archived env; the new destination starts fresh.
- **Verification toggle** as described above.

### API and permissions

- New actions (update, enable, change destination, verification toggle) are regular `/api/v1` workspace routes, like restart -- reachable by in-workspace agents through the management API by design.
- A new **target-scoped verb** in the existing `minds-workspaces` latchkey detent scope -- `minds-workspaces-backups-manage` -- gates all backup-management actions (following the `destroy`/`lifecycle` per-target pattern). The check/read side stays under the existing status/read surface.

## Changes

### forever-claude-template (separate repo, `.external_worktrees/`, sequenced first)

- `libs/host_backup`: move snapshot-mechanism detection into the service (in-memory capabilities at startup, reusing bootstrap's decision tree); drop `[snapshot]` from the `backup.toml` contract and ignore it when present; make config loading tolerant (defaults when absent, warnings for unknown/invalid keys); keep no-op compat shims in `host_backup.config` for bootstrap's imports; update README with the stable contract and the `backup-update:` commit convention.
- `libs/bootstrap`: delete `_init_backup_config`, `detect_snapshot_settings`, and template writing; stop importing `host_backup` going forward.
- Tests updated for the new detection/config semantics; changelog entry per that repo's convention. Released and tagged (`minds-v<X>`) before the minds-side feature is meaningful.

### minds app (`apps/minds`)

- New backup verification module: renders/pushes the check script, parses its JSON, applies the newer-is-fine rule and env-hash comparison, performs adoption into the canonical store, and folds results into the existing backup status batch (`backup_status.py`) with new problem states. Respects the per-workspace verification toggle and reports "offline" for unreachable workspaces.
- New backup update module: renders/pushes the single update script; runs it as a tracked workspace operation with step-level progress (waiting-for-tick, git work, uv sync, restart, verify), rollback handling, stash-conflict warning, and the chat-agent gate with "Stop all chats and retry" (stop chats via existing machinery, auto-retry).
- `backup_provisioning.py` / `backup_env_store.py`: workspace-side env rotation to `restic.env.<date>` before overwriting; minds-side canonical-env archiving; destination-change flow as fresh provisioning; adoption write path.
- Per-workspace verification-enabled flag in the minds-side store (default enabled).
- `api_v1.py`: expanded status payload (per-check breakdown, installed/desired versions, offline/unverifiable states); new routes for update, enable, change destination, and the verification toggle; serialized with other per-workspace operations.
- Latchkey: add the `minds-workspaces-backups-manage` target-scoped verb (dialog metadata in `mngr_latchkey.workspace_permissions`, schema construction in the gateway extension, per the existing verb pattern).
- UI: warning badge with tooltip on the workspace tile (one style for all problem states, reusing the `is-stale` dot pattern); workspace settings backup section with per-check breakdown, version strings, the single "Update backup service" button, operation progress with cancel, "Stop all chats and retry", the enable / change-destination forms, and the verification toggle.

### Tests & changelog

- Release-tier e2e test of the core loop: create a workspace at an older ref, drift detected, update applied, service verified RUNNING. Sibling release-tier tests for enable-on-CONFIGURE_LATER, env repair, and destination change.
- Unit/integration coverage: check-script output parsing and state classification (incl. newer-is-fine and unverifiable), adoption, env rotation and canonical archiving, gate logic (chat vs system/worker agents), rollback path, tolerant `backup.toml` parsing and capability detection (FCT side), latchkey verb metadata.
- Changelog entries in every touched project in both repos (`apps/minds`, `libs/mngr_latchkey` if touched, `dev` for the spec; FCT projects per that repo's convention).
