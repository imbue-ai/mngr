# Host-dir backup service (restic + R2)

## Overview

- A new long-running service inside every mind workspace continuously backs up the full `host_dir` (`/mngr/`) to a remote restic repository (Cloudflare R2 by default), using a per-tick btrfs subvolume snapshot on lima/vps-docker providers and a direct read on plain docker.
- The service lives in a new `libs/host_backup/` library in the forever-claude-template repo, registered as `[services.host-backup]` in `services.toml` with `restart = "on-failure"`; the existing bootstrap service manager spawns the `svc-host-backup` tmux window via its normal reconcile loop — no new tmux mechanism, no changes to how services are launched.
- All script-side config (frequency, retention, exclude patterns, snapshot method) lives in `runtime/backup.toml`; all restic secrets (R2 access keys, encryption password) live in `runtime/secrets/restic.env`. The bootstrap manager writes both files at first boot — the env file is an empty template the user must populate before backups actually run.
- The script tolerates every error (loop never exits, all exceptions logged to loguru + jsonl with full traceback, hard 1-minute minimum gap between attempts), self-heals on missing secrets / missing repo (runs `restic init` on demand), and reacts immediately to config edits via a 15-second mtime poll on both config files.
- The btrfs subvolume snapshot lives at `<btrfs-mount>/snapshots/current/` (single slot, no datetime suffix) so restic gets stable inode/path caching across runs; on vps-docker the inner script asks a small outer systemd helper (shipped via `mngr_vps_docker` cloud-init) to do the actual btrfs op via a single-slot `request.json` / `result.json` file protocol; on lima the script runs `sudo btrfs subvolume snapshot` directly. The existing `runtime-backup` (git push of `runtime/` to GitHub) is orthogonal and unchanged.

## Expected Behavior

### Service lifecycle

- On container boot, `bootstrap/manager.py` runs a new pre-services init step that:
  - Detects the snapshot method by probing the filesystem: `/mngr-snapshot/` volume present → `outer_trigger`; `findmnt -n -o FSTYPE /mngr` returns `btrfs` → `btrfs_local`; otherwise → `direct`.
  - On every boot, rewrites `runtime/backup.toml`'s `snapshot.method` and its associated paths to match the detected environment (preserves user-customized fields like retention, excludes, repo URL via toml-merge so a migrated workspace recovers automatically; only the environment-derived `snapshot` section is overwritten).
  - Writes `runtime/secrets/restic.env` only if it does not already exist — empty/comment-only template with placeholders for the three required keys.
  - Completes before the services reconcile loop starts the `svc-host-backup` window.
- `bootstrap/manager.py` already runs `_init_runtime_worktree` first, so `runtime/` is a worktree of `mindsbackup/$MNGR_AGENT_ID` by the time the new init step writes `backup.toml` into it; `backup.toml` therefore rides the runtime-backup git push to GitHub, while `runtime/secrets/restic.env` is excluded by the existing `secrets/` entry in `runtime/.gitignore`.
- The `host-backup` service starts in tmux window `svc-host-backup`; if it ever exits unexpectedly, `restart = "on-failure"` brings it back.
- The script writes structured events to `$MNGR_AGENT_STATE_DIR/events/backup/events.jsonl` with these types: `backup_started`, `snapshot_created`, `restic_backup_succeeded`, `restic_backup_failed`, `forget_completed`, `prune_completed`, `prune_skipped`, `config_reloaded`, `repo_init_attempted`, `repo_init_succeeded`, `tick_skipped_due_to_missing_secrets`, `tick_error`. Full stdout/stderr of every restic command is captured into the same stream for forensic debugging.
- The script never writes a separate `backup-status.json` file — current state is derivable from the jsonl event log (or queried directly from restic on demand).

### Tick loop

- Each tick runs the following sequence, holding `backup_in_progress = True` for the duration:
  1. Reload `backup.toml` (emit `config_reloaded`); confirm `restic.env` has non-empty values for `RESTIC_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (emit `tick_skipped_due_to_missing_secrets` and abort the tick otherwise).
  2. Probe the remote repo with `restic snapshots --json`; on the specific "repository does not exist" / "unable to open config" error class, run `restic init` (emit `repo_init_attempted` then `repo_init_succeeded` on success); on any other error, log + abort tick.
  3. Take a snapshot per the `snapshot.method` setting — always delete-then-create the `current/` slot (no startup special case; deletion-of-nonexistent is tolerated as a no-op):
     - `btrfs_local`: `sudo btrfs subvolume delete <btrfs-mount>/snapshots/current` (best-effort), then `sudo btrfs subvolume snapshot -r <btrfs-mount>/<host-subvol> <btrfs-mount>/snapshots/current`.
     - `outer_trigger`: write `/mngr-snapshot/request.json` with `operation = "cleanup"` and a fresh `request_id`; wait for matching `result.json`; then write a second `request.json` with `operation = "snapshot"` and another fresh `request_id`; wait for matching `result.json`. Inner polls `result.json`'s mtime every 1s up to a configurable hard timeout (default 120s) and verifies `request_id` matches before consuming.
     - `direct`: no-op — restic reads `/mngr/` directly.
     - Emit `snapshot_created` with `method`, `snapshot_path`, and outer-helper exit_code/stdout/stderr if applicable.
  4. Run `restic backup <source-path> --tag <iso-timestamp> --exclude=<each pattern>`, with `<source-path>` being `/mngr-snapshots/current` (btrfs providers) or `/mngr` (direct). Capture stdout/stderr into a `restic_backup_succeeded` (with `snapshot_id`, `bytes_added`, `files_new` parsed from `--json` output) or `restic_backup_failed` (with `exit_code` + tail of stderr) event.
  5. Delete the `current/` snapshot (same mechanism as step 3's delete — `sudo btrfs subvolume delete` for `btrfs_local`, second `cleanup` `request.json` for `outer_trigger`, no-op for `direct`).
  6. Run `restic forget --keep-hourly N --keep-daily M --keep-weekly W --keep-monthly Mo` (values from `backup.toml`); emit `forget_completed`.
  7. If `now - mtime(runtime/last-restic-prune) >= prune_interval_hours`, run `restic prune` and update the timestamp file; emit `prune_completed` or `prune_skipped` accordingly. The timestamp file is intentionally outside `runtime/secrets/` so it rides the runtime-backup git push and survives container loss.
- Between ticks: the script polls both config files' mtimes every `config_poll_interval_seconds` (default 15). If either file changed since the last reload (or the wall-clock interval `backup_interval_seconds` has elapsed since the last tick start), kick off the next tick — but never start a tick within `minimum_backup_gap_seconds` (default 60) of the previous tick's end.
- The outer service loop never exits. Every exception (expected or unexpected) is caught at the loop boundary, logged via `logger.error` with full traceback, recorded as a `tick_error` jsonl event, and the loop continues to the next sleep.

### Manual trigger CLI

- `uv run host-backup-now` is a thin CLI that:
  1. Checks the service's `backup_in_progress` state (by reading the most-recent event of type `backup_started` / `restic_backup_succeeded`-or-`failed` in the jsonl — if the latest is `backup_started` without a corresponding completion, a backup is in progress).
  2. If a backup is in progress, blocks until the next `restic_backup_succeeded` / `restic_backup_failed` event is observed.
  3. Bumps `backup.toml`'s mtime (via `path.touch()`).
  4. Tails events.jsonl from EOF, waits for the next `restic_backup_succeeded` / `restic_backup_failed` event, prints its JSON to stdout, and exits 0 (success) or 1 (failure).
- A `--timeout` flag (default 30 min) caps how long the CLI waits.

### Snapshot path / mount layout

- On btrfs providers, the snapshot dir is at `<btrfs-mount>/snapshots/current/` and is bind-mounted into the agent container as `/mngr-snapshots/` (read-only):
  - vps-docker: a new `--volume <btrfs-mount-path>/snapshots:/mngr-snapshots:ro` is appended to the container's `docker run` args by `mngr_vps_docker._setup_container_on_vps`.
  - lima: the snapshot path lives inside the VM filesystem already; no extra mount needed (the script reads it at its absolute path).
- On plain docker (no btrfs), the script passes `/mngr` to restic directly; no snapshot dir exists.

### Outer-helper (vps-docker)

- The outer helper is a small bash script + systemd unit shipped as resource files in `libs/mngr_vps_docker/imbue/mngr_vps_docker/resources/` (`snapshot_helper.sh` + `snapshot_helper.service`); cloud-init's `write_files` block writes them to `/usr/local/sbin/snapshot_helper.sh` and `/etc/systemd/system/snapshot_helper.service` and `systemctl enable --now`s the unit at host-create time.
- A new docker volume `mngr-snapshot-trigger-<host_id_hex>` is created at host-create time and mounted into the container at `/mngr-snapshot/`. The same volume is also bind-mounted on the VPS outer at `/var/lib/mngr-snapshot/` so the helper can watch it.
- Helper protocol (single-slot files):
  - Inner writes `/mngr-snapshot/request.json.tmp` then renames to `request.json`: `{"request_id": "<uuid>", "operation": "snapshot" | "cleanup", "timestamp_iso": "..."}`.
  - Outer watches `request.json` via `inotifywait`; reads the file, dispatches to `btrfs subvolume snapshot -r <btrfs-mount>/<host_id_hex> <btrfs-mount>/snapshots/current` (for `snapshot`) or `btrfs subvolume delete <btrfs-mount>/snapshots/current` (for `cleanup`).
  - Outer writes `/var/lib/mngr-snapshot/result.json.tmp` then renames to `result.json`: `{"request_id": "<same uuid>", "operation": "...", "exit_code": int, "stdout": "...", "stderr": "...", "snapshot_path": "..."}`.
  - Inner polls `result.json`'s mtime every 1s; once mtime changes, reads the file and verifies `request_id` matches the request_id it wrote (skips stale results).

### Provider matrix

- `lima` (snapshot method = `btrfs_local`): script runs `sudo btrfs ...` directly inside the VM (agent user already has passwordless sudo, no new sudoers entry needed); reads from `<btrfs-mount>/snapshots/current/` at its absolute path.
- `mngr_vps_docker` (vultr, ovh — snapshot method = `outer_trigger`): script writes to `/mngr-snapshot/request.json`, reads from `/mngr-snapshot/result.json`; restic reads from `/mngr-snapshots/current/` (the bind mount of the outer's snapshot dir).
- `mngr_docker` (plain docker — snapshot method = `direct`): no snapshot; restic reads `/mngr/` directly. Suitable only for testing/dev because the on-disk state can shift mid-backup.
- All other providers are out of scope for v1 (backup.toml's detection falls through to `direct`, which is correct only when /mngr is locally writable; treat any non-detected provider as user-responsibility).

## Changes

### New: `libs/host_backup/` in forever-claude-template

- New library under `libs/host_backup/` with package `host_backup`, exposing two CLI entry points:
  - `host-backup` — the long-running tick loop (called from `services.toml`).
  - `host-backup-now` — the manual-trigger CLI.
- `host_backup/data_types.py`: pydantic `FrozenModel` classes for `BackupConfig` (toml-loaded), `SnapshotMethod` enum (`BTRFS_LOCAL`, `OUTER_TRIGGER`, `DIRECT`), `BackupTickResult`, `SnapshotResult`, `OuterHelperRequest`, `OuterHelperResult`, etc.
- `host_backup/interfaces.py`: `SnapshotTakerInterface` (abstract `take_snapshot()` / `delete_snapshot()`) with three concrete implementations under `host_backup/snapshot/` (one per method).
- `host_backup/runner.py`: top-level tick loop (no class — just functions); reads config, detects in-flight state, drives the per-tick sequence.
- `host_backup/events.py`: structured event types (subclass the existing `EventEnvelope` already used by `app_watcher`).
- `host_backup/cli.py`: click entry points for both binaries.
- Tests:
  - Unit tests for pure logic (config parse, mtime poll, snapshot-method composition, exclude pattern composition, retention argument building, event envelope construction).
  - Integration tests against a local `restic init -r /tmp/repo` covering the full backup → forget → prune cycle and the snapshot-method dispatch (mock SnapshotTaker implementations for non-direct methods).
  - Real-R2 acceptance testing intentionally deferred to v2.

### Modified: `libs/bootstrap/` in forever-claude-template

- `bootstrap/manager.py` gains a new pre-services init step `_init_backup_config()` that runs after `_init_runtime_worktree()` and before the services reconcile loop:
  - Detects `snapshot.method` from the filesystem.
  - Loads any existing `runtime/backup.toml` and merges its user-customized fields (retention, excludes, intervals, repo URL template, R2 account/bucket) with a freshly-computed `[snapshot]` section (always overwritten).
  - Writes `runtime/secrets/restic.env` only if absent — empty/comment-only template.
- New constants near `INITIAL_CHAT_SIGNAL`: `BACKUP_CONFIG_FILE = Path("runtime/backup.toml")`, `RESTIC_ENV_FILE = Path("runtime/secrets/restic.env")`.

### Modified: forever-claude-template root

- `services.toml`: add `[services.host-backup]` with `command = "uv run host-backup"` and `restart = "on-failure"`.
- `Dockerfile`: add `restic` to the `apt-get install` block alongside the existing tools.
- `.mngr/settings.toml`: add `restic` to the lima `extra_provision_command__extend` `apt-get install` line.

### Modified: `libs/mngr_vps_docker/` in this monorepo

- New resource files under `libs/mngr_vps_docker/imbue/mngr_vps_docker/resources/`:
  - `snapshot_helper.sh` — the outer bash script that watches `/var/lib/mngr-snapshot/request.json` via `inotifywait`, runs the requested btrfs op, writes `result.json` atomically.
  - `snapshot_helper.service` — the systemd unit that runs `snapshot_helper.sh` under `Restart=always`.
- `cloud_init.py`: add a `write_files` block that materializes those two files at `/usr/local/sbin/snapshot_helper.sh` and `/etc/systemd/system/snapshot_helper.service`, plus a `runcmd` line that `systemctl enable --now snapshot_helper.service`. The systemd unit depends on the btrfs mount existing, which `_prepare_btrfs_on_outer` already ensures.
- `instance.py` (`_setup_container_on_vps`): create a docker volume `mngr-snapshot-trigger-<host_id_hex>` and pass it via `--volume mngr-snapshot-trigger-<host_id_hex>:/mngr-snapshot/` on the container `docker run`; bind-mount the same volume's data path at `/var/lib/mngr-snapshot/` on the outer (via `--driver=local --opt type=none --opt device=/var/lib/mngr-snapshot --opt o=bind`). Also append `--volume <btrfs-mount>/snapshots:/mngr-snapshots:ro` so the in-container restic can read the snapshot.
- `destroy_host`: best-effort `docker volume rm mngr-snapshot-trigger-<host_id_hex>` alongside the existing volume cleanup.
- `mngr_vps_docker/changelog/mngr-mind-backup.md` — new per-PR changelog entry describing the outer helper.

### Out of scope for this PR

- The FCT-side changes (new `libs/host_backup/`, `bootstrap/manager.py` edits, `services.toml`, `Dockerfile`, `.mngr/settings.toml`) live in a *separate repository* (forever-claude-template) and land as a separate PR there with its own changelog entry. This monorepo PR contains only the `mngr_vps_docker` changes plus `specs/host-backup/concise.md` (which gets a `dev/changelog/mngr-mind-backup.md` entry).
- Acceptance testing against a real R2 bucket — deferred to v2.
- Restore tooling (`host-backup-restore` CLI, UI for browsing past snapshots) — deferred; users can use `restic restore` manually with the env file they already maintain.
- Per-workspace `backup.toml` customization UI in the minds desktop client — deferred.
- The lima provider does not get any Python-side changes; if a future lima refactor requires inner-VM snapshot via a helper (rather than direct `sudo btrfs`), that's a separate spec.
- Any change to the existing `runtime-backup` (git push to GitHub) service.
