- Fixed: `mngr list` / `mngr kanpan` no longer log a per-agent
  `WARNING: Error evaluating ... no such member in mapping: 'X'` when an
  `--include` / `--exclude` filter references a key on a schemaless
  field (`labels`, `plugin`, `host.tags`, `host.plugin`) that some
  agents happen not to have. The filter now quietly evaluates to false
  on those agents.
- `has(labels.foo)` (and the same for keys under `plugin`, `host.tags`,
  `host.plugin`) is now the recommended presence-check idiom for those
  schemaless fields, and is shown in the `mngr list --help` examples.
  Note: `labels.foo != null` does NOT work as a presence check on
  tolerant fields -- use `has(...)` or a direct comparison.
- Filters against typoed strict fields (e.g. `host.providr` instead
  of `host.provider`) still surface a warning so users can see the
  typo.
