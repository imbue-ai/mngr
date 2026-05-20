Restructure the changelog system from a single repo-wide changelog to one set of changelogs per project.

- Entry files now live at `changelog/<project>/<branch>.md` (project subdir per project the PR touches), instead of a single `changelog/<branch>.md`. "Project" is any `libs/<name>` or `apps/<name>` directory, plus the synthetic `dev` bucket for root-level files (scripts, CI, top-level docs, build tooling).
- The consolidator (`scripts/consolidate_changelog.py`) walks `changelog/<project>/` subdirs and routes each project's entries into `<project_dir>/UNABRIDGED_CHANGELOG.md` (i.e. `libs/<name>/UNABRIDGED_CHANGELOG.md`, `apps/<name>/UNABRIDGED_CHANGELOG.md`, or `dev/UNABRIDGED_CHANGELOG.md`). The machine-readable output format is now one `SECTION <project> <date>` line per inserted section.
- The `test_pr_has_changelog_entry` ratchet now computes the projects the PR diff touches and requires `changelog/<project>/<branch>.md` for each. Changelog artifacts (`changelog/**`, `**/CHANGELOG.md`, `**/UNABRIDGED_CHANGELOG.md`) don't trigger requirements.
- `scripts/changelog_consolidation_prompt.md` was updated to parse `SECTION` lines and summarize each project's section into that project's `CHANGELOG.md` `[Unreleased]`.
- `scripts/release.py` finalizes each bumped package's `libs/<name>/CHANGELOG.md` `[Unreleased]` section using that package's own bumped version. `apps/<name>/CHANGELOG.md` and `dev/CHANGELOG.md` are not versioned, so their `[Unreleased]` accumulates entries indefinitely.
- New shared `scripts/changelog_projects.py` owns the path-to-project mapping (used by the consolidator, the ratchet, and the release script).

The existing top-level `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` were retroactively split into per-project files; see each project's CHANGELOG.md for its history.
