# Unabridged Changelog - concurrency_group

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/concurrency_group/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-13

Background processes started with `ConcurrencyGroup.run_process_in_background()` now default to `is_checked_by_group=True`, so non-zero exits surface as `ProcessError` at group teardown instead of being silently swallowed. Pass `is_checked_by_group=False` for processes the caller terminates explicitly (e.g. via `terminate()` or a fire-and-forget timeout).
