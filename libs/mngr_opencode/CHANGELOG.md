# Changelog - mngr_opencode

A concise, human-friendly summary of changes to the `mngr_opencode` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: opencode agents now preserve transcripts (raw + common) and the root session-id history on destroy, matching the claude plugin. New `preserve_on_destroy` config option (default `true`) — copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`. Works for both online destroys and offline host destruction. opencode's native resumable session store (the SQLite `opencode.db` plus its `-wal`/`-shm` write-ahead-log sidecars and `storage/`) is preserved too, so the session can be resumed/adopted; the sibling `auth.json` (a symlink to shared credentials) and `log/` are excluded. WAL sidecars are copied alongside the db so recent (not-yet-checkpointed) turns are not lost.

## [v0.2.15] - 2026-06-16

### Added

- Added: `waiting_reason` field in `mngr list` for opencode agents (matching claude and codex): `PERMISSIONS` while blocked on an `ask` permission prompt, `END_OF_TURN` when idle. The in-process plugin tracks opencode's `permission.asked` / `permission.replied` events (concurrent prompts handled, accepting both the binary's and the SDK's event names). The reason is gated on the agent's `active` marker via a shared `classify_waiting_reason` in mngr core so claude / codex / opencode cannot drift.

## [v0.2.14] - 2026-06-15

## [v0.2.13] - 2026-06-13

### Added

- Added: Real OpenCode agent-type support at roughly `mngr_antigravity` parity (previously a bare config shell that ran the binary but reported WAITING forever, with no transcript, resume, or isolation). Each agent runs a headless `opencode serve` plus an `opencode attach` TUI client, driven by an in-process TypeScript plugin over the server's HTTP API: subagent-aware RUNNING/WAITING lifecycle, conversation resume across `mngr stop`/`start`, in-process raw and common transcripts (`emit_common_transcript`, default on), per-agent isolation (`OPENCODE_CONFIG_DIR`/`XDG_DATA_HOME`), and shared auth (the per-agent `auth.json` symlinks to the user's global login; `symlink_auth = false` for full isolation).
- Added: `opencode` agent-type config options (`config_overrides`, `sync_global_config`, `symlink_auth`, `auto_allow_permissions`, `emit_common_transcript`), plus a conformance test asserting opencode's emitted common-transcript records validate against the canonical envelope schema (`imbue.mngr.agents.common_transcript_records`), run on the shared agent release-lifecycle harness.

## [v0.2.12] - 2026-06-08

## [v0.2.11] - 2026-06-05

## [v0.2.10] - 2026-06-01

## [v0.2.9] - 2026-05-28
