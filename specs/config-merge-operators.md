# Config-merge operators: the implementation reference

Status: **implementation reference (in progress).** Pins the final operator semantics for the
`__assign` + `Static*` + phased-resolution + `merge`/`finalize` work landing now. The
flag-removal / raise-immediately piece is **deferred** (see "Deferred" at the bottom) pending a
team decision, so everything here is **additive** — it coexists with the existing
`allow_settings_key_assignment_narrowing` flag. Seed code: `libs/mngr/imbue/mngr/config/`
(`key_resolver_primitives.py`, `key_resolver.py`, `data_types.py`); will be extracted into a
standalone `overlay` package afterward (see [layered-config-merge-lib-readme](./layered-config-merge-lib-readme.md)).

## Operators (key suffixes)

- **bare `key`** -> **assign**: replace the value from the layer below. **Narrowing-checked**
  (records a violation if it drops a non-empty aggregate entry).
- **`key__extend`** -> **merge** onto the layer below: list concat, set union, **recursive**
  dict merge (a nested `key__extend` recurses; a nested bare key assigns at its level; bare
  values resolve against an empty base so inner markers collapse). **Never narrows** (superset).
- **`key__assign`** -> **assign WITHOUT the narrowing check**: explicit "I am replacing this,
  don't warn." Behaviorally identical to bare assign except no narrowing violation is recorded.

## `Static*` markers

`StaticTuple` / `StaticList` / `StaticDict`: wrap an aggregate value to mark it **atomic** --
replacing it is a value-set, not aggregate narrowing, so it is exempt from the narrowing
check. mngr's existing `StringDerivedTuple` (a TOML string like `cli_args = "--a --b"` coerced
into a tuple) **becomes a `StaticTuple` subclass**, preserving today's "string-form is a whole
value, not narrowing" behavior as a reusable concept rather than a special case in the
narrowing walker.

## Within-layer resolution (order-independent, two phases)

A single layer resolves in two phases, regardless of key order (key order is unreliable for
unordered sources like env vars):

1. **assign-phase**: apply bare keys and `__assign` keys.
2. **extend-phase**: apply `__extend` keys onto the assign-phase result.

So `key` + `key__extend` = "reset then add"; `key__assign` + `key__extend` = "reset-without-
warning then add". `__assign` lives in the assign-phase (it is an assign), **before**
`__extend`, which is what makes `key__assign` + `key__extend` mean "reset-no-warn then add"
rather than "extend then discard".

**One error:** bare `key` **and** `key__assign` for the same key in the same layer
(two contradictory assigns). Raise a clear `ConfigParseError`. (Two `key__assign` or two
`key__extend` can't occur -- duplicate dict key.)

## `merge(lower, higher) -> (patch, narrowings)`

Combine two patches, `higher` over `lower`. Pure, recursive, associative, **never raises**.

- Preserves a `__extend` marker that has nothing concrete to resolve against (so it can
  resolve later against a runtime base); combines two markers for the same key; a higher
  **bare** (or `__assign`) key wins over a lower marker.
- **Narrowing**: records a dotted path wherever a **bare** assign drops a non-empty aggregate
  entry, **recursively** (including bare keys nested inside an `__extend`). Suppressed for:
  `__assign` keys and `Static*` values. **Not** gated on the global flag -- the flag is applied
  by the *caller*, not the algebra (keeps the algebra a pure total function; see "coexistence"
  below).
- Associativity contract (property-tested): `finalize(merge(merge(B, X), Y)) ==
  finalize(merge(B, merge(X, Y)))`, and folding layers in any grouping yields the same result.

This replaces `combine_patches` (the no-narrowing combine) **and** `fold_settings_patch` (the
resolve-against-base + narrowing). `merge` is both: against a concrete base every marker
resolves; against a patch unresolvable markers survive.

## `finalize(patch) -> dict`

Resolve any remaining `__extend` against nothing (extend-against-empty = assign), producing a
marker-free dict. Pure. **No assertion** -- a leftover marker resolving to a bare key is the
correct "nothing to extend" behavior, not a bug; a genuinely-forgotten base shows up as
missing base keys, which ordinary tests catch.

## Coexistence with the existing flag (this PR)

`merge` records narrowings honoring `__assign` / `Static*` but **not** the global flag. Each
caller decides what to do with the returned narrowings:

- **Loader (config-load):** feed them into the existing `_collect_layer_narrowing` /
  aggregation path, which raises at the end unless `allow_settings_key_assignment_narrowing`
  (unchanged for now).
- **Provision (`_build_settings_json`):** raise if `narrowings and not allow_narrowing`
  (the flag value, as today) -- replacing the current top-level-only loop with the recursive
  `merge` narrowings.

So `__assign` is an **additional** per-key opt-out layered on top of the existing global flag;
both are honored. Nothing about the flag changes in this PR.

## Worked examples (-> tests)

- `permissions__extend = {allow__extend: [X]}` over `B={permissions:{defaultMode:D, allow:[Y]}}`
  -> `{permissions:{defaultMode:D, allow:[Y,X]}}`, no narrowing.
- `permissions = {allow:[X]}` (bare) dropping `B.permissions.defaultMode` -> narrowing recorded
  (raised unless flag).
- `permissions__assign = {allow:[X]}` dropping the same -> **no** narrowing recorded.
- bare `permissions` + `permissions__assign` (same layer) -> `ConfigParseError`.
- nested bare drop inside an extend: `permissions__extend = {allow: [X]}` where bare `allow`
  replaces a non-empty `B.permissions.allow` -> narrowing recorded at `permissions.allow`
  (the recursive-narrowing fix).
- `StaticTuple(("--a","--b"))` replacing a non-empty list -> no narrowing.
- associativity: `merge(merge(B,X),Y)` finalized == `merge(B, merge(X,Y))` finalized, for the
  four combine cases + nested dicts.

## Deferred (NOT this PR -- pending team decision)

Removing `allow_settings_key_assignment_narrowing` and switching narrowing to **raise-
immediately** (dropping the loader's `_collect_layer_narrowing` aggregate-and-defer machinery)
is held until the team settles the narrowing-philosophy question -- note the in-flight plan to
default the flag to `True` (silence narrowing by default), which `__assign` deliberately
reverses (keep the signal, opt out per key). When approved, that change is isolated: delete the
flag, make `merge`'s callers raise on the spot (the flag is the only reason aggregation
exists), update the few configs/tests/e2e that set the flag to use `__assign`. Until then the
flag and `__assign` coexist per "Coexistence" above.

## Future direction: typed node wrappers instead of string suffixes

Idea (captured, not yet designed): rather than encoding operators as key-string suffixes
(`key__extend` / `key__assign`) parsed at merge time, represent them as **typed wrapper
objects** around node values -- e.g. `Extend(value)`, `Assign(value)`, `Default(value)` (and
the `Static*` markers already are this shape). The merge algebra would dispatch on the wrapper
TYPE, not parse strings. Benefits: no string parsing, no `__`-suffix collision rules, operators
are first-class/typed, and the `Static*` markers unify with the operators under one "tagged
node" model. The string-suffix form stays as the TOML/env/`--setting` surface syntax (users
still type `key__extend`); a thin parse step lifts those into the typed wrappers at the
config-load boundary, and everything downstream (combine/merge/finalize/narrowing) works on
typed nodes. Worth designing when the `overlay` library is extracted -- the lib's core would
be the typed-node algebra; the suffix parsing becomes a consumer-side (or helper) adapter.
