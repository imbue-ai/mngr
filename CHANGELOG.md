# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-21

- Added a per-PR changelog system: contributors add an entry file under `changelog/` (enforced by a CI ratchet test), and a nightly agent consolidates them into `UNABRIDGED_CHANGELOG.md` and an AI-summarized `CHANGELOG.md` via an idempotent setup script (`scripts/setup_changelog_agent.sh`).

