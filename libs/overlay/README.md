# overlay

A small, **total, dependency-free** library for merging **layered configuration**. It
operates purely on plain dicts / lists / sets / scalars plus a tiny set of key-suffix
operators and atomic-value markers. It knows nothing about your config framework
(pydantic, dataclasses), your file formats (TOML, YAML, env vars, CLI flags), or your
schema. You compile your config into its operator language, it merges, and you read the
result back and re-parse into your own types.

It owns exactly one thing: **the algebra of how N precedence-ordered layers of config
combine** -- assign vs. merge, narrowing detection, and deferred resolution against a
runtime base. Nothing else.

## Why it exists

Layered config (built-in defaults < user < project < local < env < CLI) needs a
*precise, predictable* merge. The two obvious defaults are both surprising:

- **Assign-by-default**: a higher layer's value replaces the lower's *wholesale* -- so
  setting one nested key silently drops its siblings.
- **Deep-merge-by-default**: you can never cleanly *replace* a value, and list semantics
  are ambiguous.

The fix is **explicit operators** (assign vs. extend) plus a **narrowing guard** that
flags when an assign silently drops entries from the layer below.

## Core model

### Values

Ordinary JSON-shaped data: `dict`, `list`, `set`, scalar. Plus optional **`Static*`
wrappers** (`StaticTuple`, `StaticList`, `StaticDict`): "this aggregate is **atomic** --
replacing it is a value-set, not narrowing." `ScalarTuple` is a `StaticTuple` subclass
for a tuple-typed value that is semantically a single scalar (e.g. a value written as a
single string and coerced into a tuple).

### Operators (key suffixes)

- **bare `key`** -> **assign**: replace the value from the layer below.
  Narrowing-checked.
- **`key__extend`** -> **merge** onto the layer below: list concat, set union,
  **recursive** dict merge (nested `__extend` go deeper; nested *bare* keys assign at
  their level). Never narrows -- an extend is always a superset.
- **`key__assign`** -> **assign without the narrowing warning**: an explicit "yes, I am
  replacing this, I know it drops things."

`EXTEND_SUFFIX` (`__extend`), `EXTEND_SUFFIX_ENV` (`__EXTEND`), and `ASSIGN_SUFFIX`
(`__assign`) are exported for surfaces that need to recognise or emit the suffixes.

### Within-layer resolution (order-independent)

A single layer is resolved in two phases: **assign-phase** (bare keys and `__assign`)
then **extend-phase** (`__extend`). This is deliberately **independent of key order** --
important because some sources are unordered (env vars have no document order). Exactly
one combination is an error: a bare `key` **and** `key__assign` for the same key in the
same layer (two contradictory assigns); `check_no_conflicting_assign` raises
`OverlayError` on it. Everything else falls out mechanically:

- `key` + `key__extend` -> assign then extend = "reset, then add".
- `key__assign` + `key__extend` -> no-warn assign then extend = "reset-without-warning,
  then add".

### Narrowing

A **bare** assign that drops a non-empty aggregate entry from the layer below is a
*narrowing*, recorded with its dotted path -- **recursively**, including bare keys nested
inside an `__extend` value. `__extend` never narrows (superset); `__assign` and
`Static*` values suppress it (`would_assignment_narrow` honours both). The library only
**reports** narrowings; **the caller decides when and whether to raise.** There is no
global "allow narrowing" flag -- suppression is *per-key* via `__assign`.

## Operations

### `merge(lower, higher) -> (patch, narrowings)`

Combine two patches, `higher` over `lower`. **Preserves** any `__extend` marker that has
nothing concrete to resolve against (so it can resolve later against a runtime base);
**combines** two markers for the same key; a higher **bare** key wins over a lower
marker. Records narrowings. **Pure. Associative** --
`finalize(merge(merge(B, X), Y)) == finalize(merge(B, merge(X, Y)))` -- so layers can be
combined in any grouping, and a runtime base supplied early or late gives the same
result. Raises only on the bare-plus-`__assign` conflict (a parse error).

### `finalize(patch) -> dict`

Resolve any **remaining** `__extend` against nothing (extend-against-empty = assign),
producing a **marker-free** dict. Pure.

### Lower-level building blocks

`apply_extend` / `extend_dict` resolve a single `__extend` value against a concrete
value; `combine_patches` is the marker-preserving combine that `merge` wraps with
narrowing detection. These are exported for consumers that need the pieces directly.

### Pipeline

Fold your layers low -> high with `merge` (put a concrete runtime base at the **bottom**
if you have one), then `finalize` once, and raise on the accumulated `narrowings` per
your policy. Associativity means you can pre-combine the layers available at config-load
time and `merge` a later-arriving runtime base onto the front -- identical result.

## What the library does NOT do (the consumer's job)

- Parse files / env / CLI into dicts.
- Own your schema or types. Serialize your config object to a dict *before* and re-parse
  *after*; your type system re-coerces declared types.
- Decide which fields behave specially -- that is encoded as suffixes/markers by the
  consumer's own pre-processing.
- Decide when to surface narrowing errors -- the lib reports; you raise.

## Properties

- `merge` is **pure, deterministic, associative**.
- Within-layer resolution is **order-independent** (safe for unordered sources).
- **Dependency-free** (stdlib only).
- **Total**: no escape hatches, hooks, or policy parameters -- all behavior lives in the
  operator language, which makes it trivially testable in isolation.
