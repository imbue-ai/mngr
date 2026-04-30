# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-04-30

- Introduced a changelog system that requires a per-PR entry under `changelog/`, enforced by a meta ratchet test in CI.
- Added nightly automation that consolidates per-PR entries into `UNABRIDGED_CHANGELOG.md` and a summarized `CHANGELOG.md`.
- Shipped an idempotent setup script (`scripts/setup_changelog_agent.sh`) for provisioning the consolidation agent.
