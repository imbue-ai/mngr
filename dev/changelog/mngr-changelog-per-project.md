Restructure the changelog system from a single repo-wide changelog to one set of changelog artifacts per project, owned inside each project's own directory.

- Each project (every `libs/<name>` and `apps/<name>`, plus the synthetic top-level `dev/`) now holds three things at its root: `changelog/` (per-PR entry files), `CHANGELOG.md` (concise summary), and `UNABRIDGED_CHANGELOG.md` (verbatim per-date sections).
- Per-PR entry files now live at `<project_dir>/changelog/<branch>.md` (one per project the PR touches), instead of a single `changelog/<branch>.md` at the repo root.
- The consolidator (`scripts/consolidate_changelog.py`) walks each project's `<project_dir>/changelog/` and routes its entries into `<project_dir>/UNABRIDGED_CHANGELOG.md`. The machine-readable output format is now one `SECTION <project> <date>` line per inserted section.
- The `test_pr_has_changelog_entry` ratchet now computes the projects the PR diff touches and requires `<project_dir>/changelog/<branch>.md` for each. Changelog artifacts (any path with a `changelog/` segment, `**/CHANGELOG.md`, `**/UNABRIDGED_CHANGELOG.md`) don't trigger requirements.
- New `test_every_project_has_changelog_layout` meta-ratchet enforces that every project has `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`, and a `changelog/` directory. Stubs were added for projects without entries yet.
- `scripts/changelog_consolidation_prompt.md` updated to parse `SECTION` lines and summarize each project's section into that project's `CHANGELOG.md` `[Unreleased]`.
- `scripts/release.py` finalizes each bumped package's `libs/<name>/CHANGELOG.md` `[Unreleased]` section using that package's own bumped version. `apps/<name>/CHANGELOG.md` and `dev/CHANGELOG.md` are not versioned, so their `[Unreleased]` accumulates entries indefinitely.
- New shared `scripts/changelog_projects.py` owns the path-to-project mapping (used by the consolidator, the ratchet, and the release script).

The existing top-level `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` were retroactively split into per-project files; see each project's `CHANGELOG.md` for its history.
