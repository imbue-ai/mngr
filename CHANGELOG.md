# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-05-03

- Introduced a changelog system: each PR adds an entry file under `changelog/`, enforced by a CI ratchet test.
- Nightly agent consolidates entries into `UNABRIDGED_CHANGELOG.md` (full) and `CHANGELOG.md` (summary).
- Added an idempotent setup script for the consolidation agent at `scripts/setup_changelog_agent.sh`.
