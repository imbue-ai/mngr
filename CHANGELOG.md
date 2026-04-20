# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-20

- Introduced a per-PR changelog system: each PR adds an entry file under `changelog/`, enforced by a CI ratchet, and a nightly agent consolidates them into `UNABRIDGED_CHANGELOG.md` and a summarized `CHANGELOG.md`.
- Added an idempotent setup script (`scripts/setup_changelog_agent.sh`) for provisioning the consolidation agent.
