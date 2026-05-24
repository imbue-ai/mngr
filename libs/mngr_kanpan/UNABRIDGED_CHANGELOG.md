# Unabridged Changelog - mngr_kanpan

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_kanpan/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-08

## mngr_kanpan: staleness taint semantics

Field values now track when they were computed and render dimmed when older than a configurable threshold, surfacing potentially-out-of-date data at a glance.

- Added a required `created: datetime` field to every `FieldValue`. Values derived from cached inputs inherit the oldest `created` of the inputs they actually used (taint propagation); world-derived values use the current time.
- Added `staleness_threshold_seconds` to `KanpanPluginConfig`. Defaults to 90% of `refresh_interval_seconds` so values that weren't refreshed last cycle render as stale.
- Stale cells render in dark grey via new `stale` / `stale_focus` urwid palette entries. Muted-row dimming wins over per-cell stale dimming.
- `ShellCommandConfig` now declares its cached `inputs` explicitly so shell-derived staleness can propagate correctly. Shells with no declared inputs are treated as world-fresh.
