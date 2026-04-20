# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-20

- Introduced a per-PR changelog system: entries live in `changelog/` and are enforced in CI via a meta ratchet test
- Added nightly automated consolidation into `UNABRIDGED_CHANGELOG.md` (verbatim) and `CHANGELOG.md` (AI-summarized), with an idempotent setup script at `scripts/setup_changelog_agent.sh`

