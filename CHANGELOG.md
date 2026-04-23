# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-23

- Introduced a changelog system that requires each PR to include an entry file in `changelog/`, enforced by a CI ratchet test
- Added nightly automated consolidation of entries into `UNABRIDGED_CHANGELOG.md` (full) and `CHANGELOG.md` (AI-summarized), with an idempotent setup script at `scripts/setup_changelog_agent.sh`

