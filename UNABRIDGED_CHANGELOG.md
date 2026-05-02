# Unabridged Changelog

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-02

- Added a changelog system for tracking changes across PRs
  - Per-PR changelog entry files in `changelog/` directory, enforced by CI via meta ratchet test
  - Nightly automated consolidation of changelog entries into `UNABRIDGED_CHANGELOG.md` (full entries) and `CHANGELOG.md` (concise AI-generated summary)
  - Idempotent setup script for the consolidation agent (`scripts/setup_changelog_agent.sh`)
