# Unabridged Changelog - mngr_kanpan

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_kanpan/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-08

## mngr_kanpan: staleness taint semantics

Field values now track when they were computed and render dimmed when older than a configurable threshold, surfacing potentially-out-of-date data at a glance.

- Added a required `created: datetime` field to every `FieldValue`. Values derived from cached inputs inherit the oldest `created` of the inputs they actually used (taint propagation); world-derived values use the current time.
- Added `staleness_threshold_seconds` to `KanpanPluginConfig`. Defaults to 90% of `refresh_interval_seconds` so values that weren't refreshed last cycle render as stale.
- Stale cells render in dark grey via new `stale` / `stale_focus` urwid palette entries. Muted-row dimming wins over per-cell stale dimming.
- `ShellCommandConfig` now declares its cached `inputs` explicitly so shell-derived staleness can propagate correctly. Shells with no declared inputs are treated as world-fresh.
