### Add the missing changelog/ directory to mngr_uncapped_claude

The recently added `mngr_uncapped_claude` project shipped with
`CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` but no `changelog/`
directory for per-PR entry files, which left the project out of the
uniform changelog layout that every other project follows (and failed
`test_meta_ratchets.py::test_every_project_has_changelog_layout`).

This adds the `changelog/` directory (tracked via `.gitkeep`, matching
the convention used by every other project) so the nightly consolidator
can fan per-PR entries into the project's `UNABRIDGED_CHANGELOG.md` and
`CHANGELOG.md`. No behavior of the `mngr uncapped-claude` command
changes.
