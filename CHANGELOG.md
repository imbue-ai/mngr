# Changelog

A concise, human-friendly summary of changes. Updated nightly by the changelog consolidation agent.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-05-02

- New changelog system tracks user-visible changes across PRs, with per-PR entry files in `changelog/` enforced by CI.
- A nightly agent consolidates entries into `UNABRIDGED_CHANGELOG.md` (verbatim) and this file (summarized), driven by an idempotent setup script at `scripts/setup_changelog_agent.sh`.
