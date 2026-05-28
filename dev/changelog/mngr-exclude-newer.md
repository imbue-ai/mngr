# Enforce the supply-chain cooldown via `[tool.uv] exclude-newer`, refreshed at release

- Moved the two-week dependency cooldown from a time-relative test to uv's native
  resolver enforcement. Added `[tool.uv] exclude-newer` to the root `pyproject.toml`
  (initial value `2026-05-23T00:00:00Z`), so `uv lock` simply refuses to consider any
  package version uploaded after the cutoff. This is proactive (you cannot lock a
  too-new package) rather than after-the-fact detection.
- `scripts/release.py` now advances the cutoff at each release: it sets
  `exclude-newer` to (today's UTC date - 2 weeks) just before regenerating
  `uv.lock`, and commits the root `pyproject.toml` alongside the version bumps. The
  update is **forward-only** -- it takes `max(current_cutoff, release_date - 2 weeks)`,
  so a release cut while the current cutoff is still younger than two weeks leaves it
  untouched rather than pushing it back. This avoids re-excluding a deliberately-pinned
  fresh dependency and breaking resolution. The
  initial value is set to just past the newest locked package for the same reason,
  which makes per-package exemptions unnecessary.
- Removed `test_no_dependencies_younger_than_two_weeks` (and its
  `_FRESHNESS_EXEMPT_PACKAGES` / `_lock_package_upload_time` helpers) from
  `test_meta_ratchets.py`; uv now enforces the cooldown at lock time, so the test is
  redundant. Its `ty`/`modal` exemptions are no longer needed because the cutoff is
  kept recent enough to admit them directly.
- Added unit tests (`scripts/release_test.py`) covering the forward-only advance, the
  no-op when the cutoff is still within the window, and the boundary case.
- The cooldown does not protect against a compromise that stays undetected past the
  window; its only value is the detection delay before we adopt a release.
