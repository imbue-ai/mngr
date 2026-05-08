# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-05-08

- Introduced a per-PR changelog system, with CI enforcement that every PR includes an entry file in `changelog/`.
- Added nightly automation that consolidates entries into `UNABRIDGED_CHANGELOG.md` and a summarized `CHANGELOG.md`.
- Shipped an idempotent setup script (`scripts/setup_changelog_agent.sh`) for provisioning the consolidation agent.
