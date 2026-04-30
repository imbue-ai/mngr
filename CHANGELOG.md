# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-30

- Introduced a changelog system: each PR now adds a per-branch entry under `changelog/`, enforced by a CI ratchet.
- A nightly agent consolidates those entries into `UNABRIDGED_CHANGELOG.md` (verbatim) and `CHANGELOG.md` (summarized).
- Added an idempotent setup script for the consolidation agent at `scripts/setup_changelog_agent.sh`.
