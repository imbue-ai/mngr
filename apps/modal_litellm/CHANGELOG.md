# Changelog - modal_litellm

A concise, human-friendly summary of changes for the `modal_litellm` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Project adopted the per-project changelog layout (`changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).
- Changed: LiteLLM-proxy deploys now run a Prisma schema push against the proxy's `DATABASE_URL` automatically (via a new `migrate_db` Modal Function invoked by `minds env deploy`), so a fresh tier or dev env no longer requires a manual `prisma db push` before the first virtual-key create.

### Fixed

- Fixed: README and module docstring drop the wrong `/anthropic` suffix from the documented `ANTHROPIC_BASE_URL` — the Anthropic SDK appends `/v1/messages` itself, which lands on LiteLLM's native route.
