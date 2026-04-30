# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-20

- Introduced a changelog system: each PR now ships a `changelog/` entry, enforced by a CI meta ratchet test
- Added nightly automation that consolidates entries into `UNABRIDGED_CHANGELOG.md` (verbatim) and `CHANGELOG.md` (AI-summarized)
- Added an idempotent setup script (`scripts/setup_changelog_agent.sh`) for the consolidation agent

