# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-22

- Introduced a changelog system: each PR now includes an entry file in `changelog/`, enforced by a CI ratchet test
- Added nightly automated consolidation into `UNABRIDGED_CHANGELOG.md` (full entries) and `CHANGELOG.md` (AI-generated summary), with an idempotent setup script at `scripts/setup_changelog_agent.sh`

