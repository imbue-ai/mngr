# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-26

- Introduced a changelog system: each PR now adds an entry file under `changelog/`, enforced by a CI ratchet test
- Added nightly automated consolidation that merges entries into `UNABRIDGED_CHANGELOG.md` (verbatim) and an AI-summarized `CHANGELOG.md`
- Shipped an idempotent setup script (`scripts/setup_changelog_agent.sh`) for the consolidation agent

