# Changelog - mngr_pi_coding

A concise, human-friendly summary of changes to the `mngr_pi_coding` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.8] - 2026-06-05

### Fixed

- Fixed: Remote provisioning of pi resource directories (skills/prompts/extensions/themes) now transfers with a single rsync (`host.copy_local_directory`) instead of uploading each file individually over SSH. The per-file approach opened an SFTP channel per file and did not scale to large resource sets (the same failure mode as github issue 1825).

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28

### Changed

- Changed: Plugin uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` now takes `tmux_target: TmuxWindowTarget` instead of a bare string.
