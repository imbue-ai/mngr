# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-30

- Introduced a changelog system: per-PR entries in `changelog/` are enforced by a CI ratchet, then consolidated nightly into `UNABRIDGED_CHANGELOG.md` and a summarized `CHANGELOG.md`.
- Added an idempotent setup script (`scripts/setup_changelog_agent.sh`) for the nightly consolidation agent.
