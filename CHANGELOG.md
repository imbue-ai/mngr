# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-30

- Introduced a changelog system: every PR now ships a per-PR entry under `changelog/`, with CI enforcement via a meta ratchet test.
- A nightly agent consolidates those entries into `UNABRIDGED_CHANGELOG.md` (full text) and a concise summary in `CHANGELOG.md`.
- Added an idempotent setup script (`scripts/setup_changelog_agent.sh`) for provisioning the consolidation agent.
