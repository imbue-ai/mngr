# Changelog - mngr_pi_coding

A concise, human-friendly summary of changes to the `mngr_pi_coding` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `pi` alias for the `pi-coding` agent type (`mngr create my-agent pi` is equivalent to `mngr create my-agent pi-coding`).
- Added: Real pi-coding agent lifecycle parity (a single mngr-owned pi extension loaded with `pi -e` drives everything pi has no shell hooks for):
  - `mngr list` reports RUNNING vs WAITING for pi agents (`active` marker maintained on pi's `agent_start`/`agent_end` events), and stays correct when an agent spawns a nested `pi` via its bash tool.
  - `mngr transcript <agent>` works for pi agents; raw pi message stream captured under the agent state dir. New config: `emit_common_transcript`, `emit_raw_transcript` (both default on).
  - `mngr stop` then `mngr start` resumes the same pi session with full context. New config: `resume_session` (default on).
  - Agent creation now waits on a real readiness signal (a sentinel the extension writes when pi's session loads) rather than only scraping the startup banner.
  - On remote hosts (when allowed), auto-installs pi from npm (`@earendil-works/pi-coding-agent`).
  - Messages are delivered by injecting into the live session via the lifecycle extension (`pi.sendUserMessage`) -- mngr appends each message to a per-agent inbox file watched by the extension -- instead of simulating tmux keystrokes. The TUI remains viewable (`mngr connect`); delivery is more reliable than the old paste+Enter path (pi intermittently swallowed the first Enter) and behaves the same on local and remote hosts.
  - Handles pi 0.79+'s "Trust project folder?" dialog by pre-trusting the agent's workspace (seeding pi's `trust.json`), gated like the claude/antigravity types: silent under `mngr create --yes` or the new `auto_dismiss_dialogs` config, an interactive prompt otherwise; trust is extended automatically when the source repo is already trusted.
  - Syncs the `agents/` resource dir from `~/.pi/agent/` into each agent's config dir alongside skills/prompts/extensions/themes, so an installed subagent extension finds its agent definitions.
- Added: Conformance test asserting pi's emitted common-transcript records validate against the canonical envelope schema (`imbue.mngr.agents.common_transcript_records`). Release test now runs on the shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`).

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

### Fixed

- Fixed: Remote provisioning of pi resource directories (skills/prompts/extensions/themes) now transfers with a single rsync (`host.copy_local_directory`) instead of uploading each file individually over SSH. The per-file approach opened an SFTP channel per file and did not scale to large resource sets (the same failure mode as github issue 1825).

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28

### Changed

- Changed: Plugin uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` now takes `tmux_target: TmuxWindowTarget` instead of a bare string.
