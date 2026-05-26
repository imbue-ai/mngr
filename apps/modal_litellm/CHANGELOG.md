# Changelog - modal_litellm

A concise, human-friendly summary of changes for the `modal_litellm` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: LiteLLM-proxy deploys now run a Prisma schema push against the proxy's `DATABASE_URL` automatically (via a new `migrate_db` Modal Function invoked by `minds env deploy`), so a fresh tier or dev env no longer requires a manual `prisma db push` step.

### Changed

- Changed: Adopted the new per-project changelog layout.

### Fixed

- Fixed: README + module docstring drop the wrong `/anthropic` suffix from the documented `ANTHROPIC_BASE_URL` (the Anthropic SDK appends `/v1/messages` itself, landing on LiteLLM's native Anthropic route).
- Fixed: `UNABRIDGED_CHANGELOG.md` intro now references the correct entries directory.
