# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-05-07

- Introduced a changelog system that requires each PR to include a per-PR entry file under `changelog/`, enforced by a CI ratchet test.
- Added nightly automation that consolidates per-PR entries into `UNABRIDGED_CHANGELOG.md` and generates a concise summary in `CHANGELOG.md`.
- Added an idempotent setup script (`scripts/setup_changelog_agent.sh`) for provisioning the consolidation agent.
