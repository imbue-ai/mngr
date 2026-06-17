# Changelog - mngr_pi_coding

A concise, human-friendly summary of changes to the `mngr_pi_coding` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.13] - 2026-06-16

### Added

- Added: pi-coding agents now preserve transcripts (raw + common), the recorded session-file pointer, and pi's native resumable session store (`plugin/pi_coding/sessions`) on destroy — so the conversation content itself now survives (previously only the dangling pointer was kept once the store was deleted). New `preserve_on_destroy` config option (default `true`) — copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`. Works for both online destroys and offline host destruction. The credential `auth.json` is a path-separate sibling and is excluded.
- Added: pi lifecycle extension now writes per-message usage events (cost + tokens) for `mngr usage`, gated on a `pi_emit_usage` marker that the new `imbue-mngr-pi-coding-usage` package provisions. Inert unless the gate marker is present, so behavior is unchanged for agents without the usage plugin installed.

## [v0.1.12] - 2026-06-16

## [v0.1.11] - 2026-06-15

## [v0.1.10] - 2026-06-13

### Added

- Added: `pi` alias for the `pi-coding` agent type (`mngr create my-agent pi` is equivalent to `mngr create my-agent pi-coding`).
- Added: Real pi-coding lifecycle parity, driven by a single mngr-owned pi extension loaded with `pi -e` (pi has no shell hooks): `mngr list` RUNNING/WAITING (correct even when an agent spawns a nested `pi`), `mngr transcript` with raw + common transcripts (`emit_common_transcript` / `emit_raw_transcript`, default on), `mngr stop`/`start` session resume (`resume_session`, default on), a real readiness sentinel instead of banner-scraping, remote npm auto-install (`@earendil-works/pi-coding-agent`), reliable message delivery via a watched per-agent inbox file (`pi.sendUserMessage`) instead of tmux keystrokes (TUI still viewable via `mngr connect`), pi 0.79+ "Trust project folder?" handling by pre-seeding `trust.json` (gated like claude/antigravity; silent under `--yes` / `auto_dismiss_dialogs`, else prompts), and syncing the `agents/` resource dir from `~/.pi/agent/`.
- Added: Conformance test asserting pi's emitted common-transcript records validate against the canonical envelope schema (`imbue.mngr.agents.common_transcript_records`), run on the shared agent release-lifecycle harness.

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

### Fixed

- Fixed: Remote provisioning of pi resource directories (skills/prompts/extensions/themes) now transfers with a single rsync (`host.copy_local_directory`) instead of uploading each file individually over SSH. The per-file approach opened an SFTP channel per file and did not scale to large resource sets (the same failure mode as github issue 1825).

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28

### Changed

- Changed: Plugin uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` now takes `tmux_target: TmuxWindowTarget` instead of a bare string.
