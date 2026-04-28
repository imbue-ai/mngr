# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-28

- Introduced a per-PR changelog system: each PR now includes an entry in `changelog/`, enforced by a CI ratchet test
- Added a nightly agent that consolidates entries into `UNABRIDGED_CHANGELOG.md` (verbatim) and `CHANGELOG.md` (AI-summarized), with an idempotent setup script at `scripts/setup_changelog_agent.sh`

