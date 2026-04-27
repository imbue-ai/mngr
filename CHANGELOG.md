# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-27

- Introduced a per-PR changelog system: each PR adds an entry under `changelog/`, enforced in CI by a meta ratchet test
- Added nightly automated consolidation that rolls entries into `UNABRIDGED_CHANGELOG.md` (verbatim) and `CHANGELOG.md` (AI-summarized)
- Added an idempotent setup script (`scripts/setup_changelog_agent.sh`) for provisioning the consolidation agent

