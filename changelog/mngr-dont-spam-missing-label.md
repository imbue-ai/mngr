- Fixed: `mngr list` / `mngr kanpan` no longer log a per-agent
  `WARNING: Error evaluating ... no such member in mapping: 'X'` when an
  `--include` / `--exclude` filter references a key on a *schemaless*
  field (`labels`, `plugin`, `host.tags`, `host.plugin`) that some agents
  happen not to have.
- The `has(map.key)` macro now correctly distinguishes present from
  absent keys on those schemaless fields, so `--include 'has(labels.foo)'`
  matches only agents that actually have a `foo` label set (previously
  this would have matched every agent or warned per agent depending on
  the underlying cel-python version). `has()` is now the recommended
  presence-check idiom for schemaless fields; `field != null` does *not*
  work as a presence check on tolerant fields and is documented as such.
- Filters against typoed *strict* fields (e.g. `host.providr`) still
  surface a warning so users can see the typo.
- Implementation: a small `TolerantMapType` in `imbue.mngr.utils.cel_utils`
  that returns a `CELEvalError` value (rather than raising) on missing
  keys, so cel-python's evaluator carries the error through equality /
  boolean / `has()` operators and short-circuits cleanly. Schemaless
  fields opt in via `tolerant_dict()`; everything else stays strict.
