# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-05-04

- Introduced a changelog system: each PR adds an entry under `changelog/`, enforced by a CI ratchet
- Nightly agent consolidates per-PR entries into `UNABRIDGED_CHANGELOG.md` and a summarized `CHANGELOG.md`
- Added `scripts/setup_changelog_agent.sh` to idempotently provision the consolidation agent
