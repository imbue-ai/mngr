Hardened suspicious edge-case handling in `mngr usage`:

- Config layering: removed `UsagePluginConfig.merge_with`, which re-derived the
  merge incorrectly (its `x if x is not None` checks were no-ops on non-Optional
  fields, so a higher config layer silently reset lower-layer `max_age_seconds` /
  `since_seconds` back to defaults) and silently dropped a mismatched override.
  `mngr usage` now inherits the canonical `PluginConfig.merge_with`, which merges
  by explicitly-set fields like every other plugin.
- Events with an unparseable `timestamp` are now dropped with a WARNING (naming
  source + event_id) instead of vanishing silently, so a malformed timestamp can
  no longer undercount cost without notice.
- A `rate_limits` window whose value isn't a dict is now logged when skipped,
  matching the existing per-window validation-error log.
- `mngr usage wait` now raises on the impossible "neither matched nor timed out"
  result for every output format (previously only the human format crashed; JSON
  / JSONL silently emitted a self-contradictory record).
