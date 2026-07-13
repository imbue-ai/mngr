# Backup Update Fixes: Per-Workspace Health, Master-Password Hash, Fixed Minimum Version

Follow-ups to the injected-backup-service work (`specs/injected-backup-service/concise.md`), landing as one monorepo PR on `mngr/backup-update-fixes` plus one paired forever-claude-template PR (via the `.external_worktrees/` convention), developed together.

## Overview

- Kill the batch backup-health surface: the backup-service verification check runs inside the existing per-workspace `GET /api/v1/workspaces/<id>/backups` route (snapshot listing and service check in parallel, a two-thread concurrency group), and cross-workspace parallelism becomes the frontend's job. The batch `GET /api/v1/workspaces/backup-health` route -- the odd non-workspace-specific one out -- is deleted.
- Rework master-password handling around a stored *secure hash*: `backup_password_hash` (argon2) always exists and is the validation authority; the plaintext `backup_password` file is NOT replaced -- it remains strictly as the optional "don't make me retype it" convenience. The password (possibly empty) is *entered* wherever a repo gets initialized and *changed* only from a global Settings-page flow that rekeys all existing workspaces. The `NO_PASSWORD`/`MASTER_PASSWORD` encryption dropdown disappears entirely: an empty master password is just an empty string, not a mode.
- Stop churning the drift check on every release: a *fixed* minimum required backup version (`minds-v0.3.4`) drives the warning; anything at that version or newer is fine. The update action still converges to the tag matching the current minds release, and "Update backup service" is always offered as an idempotent force-reset, even when already converged.
- The workspace backup scripts use a hardcoded `official` git remote (`https://github.com/imbue-ai/forever-claude-template.git`) instead of an `upstream` remote derived from `parent.toml`, reserving the `upstream` name for other purposes.
- The backup release tests are rewritten (not ported) as `minds_snapshot_resume` tests against the real baked workspace container, and the stale snapshot-image-id / "one-off prototype" comments in `scripts/snapshot_minds_e2e_state.py` are brought up to date.

## Expected Behavior

### Per-workspace backup health (task 1)

- `GET /api/v1/workspaces/<id>/backups` returns the snapshot list + `is_backing_up` *and* the service-check breakdown (`check_state`, `problems`, installed / minimum-required / update-target versions, detail, `is_verification_enabled`). The two halves run concurrently in a two-thread concurrency group inside the route.
- A workspace with no backups configured returns 200 with an empty snapshot list and the `NOT_CONFIGURED` problem (previously a 501). The check's DISABLED / OFFLINE classifications behave as before.
- No internal time budget for the check inside the route: a slow check (e.g. first-encounter tag fetch) delays only that workspace's response, and the frontend already treats these fetches as background fills.
- The batch route, `compute_backup_health`, `compute_backup_service_checks`, and their batch-budget/UNKNOWN machinery are deleted. The sidebar/chrome badge cache (`backup_health.js`) and the workspace-settings backup section fan out one per-workspace request each, on the frontend.
- The Landing "download backup" flow stops pre-fetching `/backups` to find the newest snapshot: `latest` becomes a valid `<snapshot_id>` on the existing export route (restic resolves `latest` natively) and the flow POSTs `.../backups/latest/export` directly.

### Master password: hash + optional saved plaintext (task 2)

- `backup_password_hash` (argon2, via the `argon2-cffi` library) lives beside `backup_password` under the minds env data dir. On app startup, if the hash file is missing: seed it from an existing plaintext `backup_password` when one is present, else write the hash of the empty string. The app therefore starts in the "empty master password" state and a new user can hit Create immediately.
- The master password is required exactly where a repository gets initialized -- create-with-a-real-provider, enable-backups, and change-destination, for both `IMBUE_CLOUD` and `API_KEY` -- and nowhere else (updating the backup service, toggling verification, etc. never ask). Validation: the typed value must match `backup_password_hash`; a blank submission falls back to the saved plaintext copy when one exists ("leave blank to use your saved password"), and with no saved copy blank means the empty password (also validated against the hash). A wrong password is a 400 field error.
- Forms: the encryption-method dropdown is removed from the create form and the workspace-settings configure form (`BackupEncryptionMethod` and its request fields are removed entirely). A master-password field is shown whenever a real backup provider is selected (hidden for `configure_later`) -- no conditional show/hide based on hash state -- with a "save this password" checkbox beside it. The create form's password section is about *entering* the password, never changing it.
- `save_password` / `backup_save_password` keep their wire names but change semantics: honored only after the typed password validated against the hash, they persist the plaintext convenience copy. They can never establish or change the master password.
- Changing the master password is a machine-global flow on the app Settings page (`Settings.jinja`), served by a desktop-only `/_chrome`-style route (cookie auth, not `/api/v1`) so in-workspace agents can never rotate it. The user types the new password twice (no current-password prompt; empty is a valid new password). The POST is synchronous with a generous timeout and returns per-workspace results rendered inline.
- Rotation mechanics, per currently-existing workspace with a canonical env (destroyed workspaces are skipped; their repos stay reachable under the old password via their canonical envs): authenticate with the workspace's own random password, `restic key add` the new master password, then remove every other key so the repo ends in a clean two-key state (workspace key + new master key). Failures are reported per workspace and the flow is idempotently re-runnable. On success the hash file is updated; a stale saved plaintext is deleted (the flow's own save checkbox re-saves the new value when requested).
- Agents never touch the master password: the FCT `minds-api` skill (and related docs) is updated to state that agents must never ask the user for the master password and must always create workspaces with backups unconfigured (`configure_later`). The create API tolerates an absent/empty password; there is no server-side rejection -- hash validation naturally rejects anything an agent cannot authorize.
- Security audit: every master-password field on request models (`BackupServiceConfigureRequest.master_password`, `CreateWorkspaceRequest.backup_master_password`) becomes `SecretStr`, and the plaintext value is never logged -- audit handlers, log statements, and error messages along the whole path.

### Fixed minimum backup version (task 3)

- A code constant (default `minds-v0.3.4`), overridable via an env var (e.g. `MINDS_MINIMUM_BACKUP_TAG`) for dev/testing, is the *minimum required backup version*. The drift check flags `CODE_OUTDATED` only when the workspace's `libs/host_backup` content is below it; content matching the minimum tag, or where the minimum tag is an ancestor of HEAD ("newer is fine"), produces no warning. No highest-tag fallback for the minimum: if the tag is missing even after fetching from `official`, the check reports UNVERIFIABLE.
- The *update* still converges to `minds-v<current minds release>`, with the existing preferred-tag-then-highest-tag fallback (dev builds report `0.0.0+unknown` and rely on it).
- "Update backup service" is always offered in the workspace settings -- even when already at the target version -- as an idempotent way to reset the backup service (commit only lands when content changed; the service restart/verify always runs).
- The settings breakdown shows installed, minimum required, and the update target when they differ.

### Official remote (task 4)

- The check/update workspace scripts ensure a git remote named `official` exists and points at the hardcoded constant URL, idempotently (`git remote add`, or `set-url` when it exists with a different URL -- minds owns this remote name). `parent.toml` is no longer consulted by the backup machinery.
- Only the minds backup scripts change; FCT's `update-self` / `submit-upstream-changes` skills keep using `upstream` + `parent.toml`.

### Tests (task 5)

- `test_backup_service_release.py` is deleted. Its functionality is *rewritten* (not literally migrated) as `minds_snapshot_resume` tests in `test_snapshot_resume.py`, driven against the resumed real workspace container: real supervisord and `host-backup` program, real FCT git repo, real tag fetches from the `official` remote on github.com (acceptable -- that is exactly the production path).
- Coverage: the check -> update -> converge loop (including force-update idempotence at the same version), and enable / env-repair / destination-change flows. The provisioning test installs restic on the sandbox host itself if the bundled binary is missing. Drive the full minds-side sequences (`run_backup_update_sequence`, provisioning) where practical; fall back to `docker exec` script-level driving where the desktop-side plumbing is disproportionate.
- Specific-test runners already exist and are documented: `just test <path>::<test_name>` locally (any mark), `just test-offload-release '--filter <name>'`, and `just test-offload-minds-snapshot <image-id> '--filter <name>'` for snapshot tests.

### Snapshot-script comments (task 6)

- `scripts/snapshot_minds_e2e_state.py` drops the hardcoded `im-01...` image ids and the "one-off demonstration script" framing. It is documented as the standing producer for the `build-minds-snapshot` CI stage, with clear instructions for minting a snapshot image id manually and running individual tests against it (`just test-offload-minds-snapshot <id> '--filter <name>'`), pointing at `docs/testing-overview.md` section 1.6.

## Changes

### minds app (`apps/minds`)

- `backup_verification.py`: delete `compute_backup_health` and `compute_backup_service_checks`; the check compares against the fixed minimum tag (constant + env override) with no fallback; classification otherwise unchanged.
- `backup_workspace_scripts.py`: replace `_ensure_upstream_remote`/`parent.toml` with an idempotent `official`-remote helper (hardcoded URL, `set-url` on mismatch); the check script takes the minimum tag and the update script takes the update-target version as explicit parameters.
- `api_v1.py` / `api_models.py` / `api_schema.py`: fold the check breakdown into the `GET /workspaces/<id>/backups` response (two-thread concurrency group; 200 instead of 501 when unconfigured); delete the batch backup-health route and its models; accept `latest` on the export route; remove `BackupEncryptionMethod` and its request fields; `master_password` fields become `SecretStr`; `save_password` semantics become save-after-validation.
- `backup_password_store.py` (+ a new hash-store sibling): argon2 `backup_password_hash` read/write/validate, startup seeding (from plaintext, else empty hash); plaintext store kept as the optional convenience copy.
- New rotation module + desktop-only `/_chrome` change-password route: rekey every existing workspace's repo (add new master, strip to two keys), synchronous with per-workspace results; update hash, delete stale plaintext, optional re-save.
- `workspace_create.py` (`build_backup_request_or_error`): validate typed/blank/saved password against the hash; drop encryption-method branching; `configure_later` unchanged.
- `build_info` usage in `backup_update.py`: update-target resolution unchanged (current release tag, existing fallback).
- UI: `workspace_backups.js` + `WorkspaceSettings.jinja` (per-workspace fetch, always-visible update button, three-version display, configure form without the dropdown, password field + save checkbox, saved-password helper text); `backup_health.js` (per-workspace fan-out for the badge cache); `Landing.jinja` (direct `latest` export); `Settings.jinja` (change-password form + inline results); `Create.jinja` (dropdown removal, password row shown for real providers).
- Tests: delete `test_backup_service_release.py`; new `minds_snapshot_resume` tests (check/update/converge incl. force-update, enable/repair/destination-change with self-installed restic); unit tests for hash store, validation/fallback rules, rotation key handling, classification against the minimum tag, and the SecretStr/no-logging audit points; update `api_schema`/route tests for the new response shape.
- `scripts/snapshot_minds_e2e_state.py`: comment/docstring rewrite only (task 6).
- New dependency: `argon2-cffi`.
- Changelog entries: `apps/minds`, `dev` (this spec); `libs/mngr_latchkey` only if the verb surface actually changes (not expected).

### forever-claude-template (`.external_worktrees/forever-claude-template`)

- `minds-api` skill (and any related docs): agents never ask the user for the master password and always create workspaces with backups unconfigured.
- `libs/host_backup/README.md`: stable-contract wording updated for the `official` remote and the minimum-version/update-target tag semantics.
- Changelog entry per that repo's convention.
