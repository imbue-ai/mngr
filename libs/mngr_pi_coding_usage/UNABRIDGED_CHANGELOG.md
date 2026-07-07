# Unabridged Changelog - mngr_pi_coding_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_pi_coding_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-16

Corrected the package version back to 0.1.0 (it had been bumped to 0.1.1, but that version was never actually released).

## 2026-06-16

New package `imbue-mngr-pi-coding-usage`: cost/usage tracking for pi agents in `mngr usage`. pi reports per-message cost client-side, so it's REPORTED (not estimated) and aggregated session-incrementally. Because pi loads a single explicit extension, the per-message writer lives in mngr_pi_coding's lifecycle extension; this package owns the reader (an `aggregate_usage_source` hookimpl claiming the `pi-coding` source) and provisions a `pi_emit_usage` gate marker so the extension only emits usage events when this package (their reader) is installed.
