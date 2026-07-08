# overlay

**Consistent, principled, and expressive merging of layered settings.**

Almost every application builds its effective configuration by stacking layers in
precedence order -- built-in defaults `<` user `<` project `<` local `<` environment
`<` command line. The hard part is not reading the layers; it is *combining* them in a
way that is predictable and that lets a user say exactly what they mean. The two
obvious defaults both fail:

- **Assign-by-default** -- a higher layer's value replaces the lower's *wholesale*, so
  setting one nested key silently drops its siblings.
- **Deep-merge-by-default** -- you can never cleanly *replace* a value, and list
  semantics are ambiguous (concat? union? replace?).

`overlay` is a small, **total, dependency-free** library that fixes this with **explicit
per-key operators** (assign vs. extend), **atomic-value markers**, and a **narrowing
guard** that flags when an assign would silently drop entries from the layer below. It
operates purely on plain `dict` / `list` / `set` / scalar data. It knows nothing about
your config framework, your file formats (TOML, YAML, env vars, CLI flags), or your
schema: you compile your config into its operator language, it merges, and you read the
result back and re-parse into your own types.

It owns exactly one thing: **the algebra of how N precedence-ordered layers combine.**

## The operator language

Operators ride on **key suffixes** in the surface dict you hand the library:

- **bare `key`** -> **assign**: replace the value from the layer below. Narrowing-checked.
- **`key__extend`** -> **merge** onto the layer below: list concat, set union, and
  **recursive** dict merge (nested `__extend` go deeper; nested *bare* keys assign at
  their own level). Never narrows -- an extend is always a superset.
- **`key__assign`** -> **assign without the narrowing warning**: the explicit "yes, I am
  replacing this, I know it drops things" opt-out.

Aggregate values can be wrapped in a **`Static*` marker** (`StaticList`, `StaticDict`,
`StaticTuple`) meaning "this aggregate is **atomic** -- replacing it is a value-set, not
narrowing." `ScalarTuple` is a `StaticTuple` subclass for a tuple that is semantically a
single scalar (e.g. a value written as one string and coerced to a tuple).

The suffix constants (`EXTEND_SUFFIX` = `__extend`, `ASSIGN_SUFFIX` = `__assign`) and
key helpers are exported for surfaces that emit or recognise them. There is a single
lowercase form of each operator -- a consumer that reads a case-folded surface (e.g.
all-uppercase environment variables) normalises to it before calling the library.

### Within-layer resolution is order-independent

A single layer can mention a field more than once (`key` and `key__extend`). The library
resolves each layer in two phases -- **assign-phase** (bare keys and `__assign`) then
**extend-phase** (`__extend`) -- so the outcome never depends on key order. This matters
because some sources are unordered (environment variables have no document order).
Exactly one combination is an error: a bare `key` **and** `key__assign` for the *same*
key in the *same* layer (two contradictory assigns) raises `OverlayError`. Everything
else falls out mechanically -- `key` + `key__extend` means "reset, then add."

### Narrowing

A **bare** assign that drops a non-empty aggregate entry from the layer below is a
*narrowing*, recorded with its dotted path -- **recursively**, including a bare key
nested inside an `__extend` value. `__extend` never narrows (it is a superset); a
`__assign` or a `Static*` value suppresses the check. **The library only reports
narrowings; the caller decides when and whether to raise.** There is no global "allow
narrowing" flag -- suppression is *per-key*, via `__assign`.

## The representation: typed nodes

Internally the operator does **not** live in the key string; it lives in the **type** of
a typed-node wrapper. `lift` converts the suffix surface syntax into a `Patch` (a
`dict[str, Node]`) whose values are one of three frozen wrappers:

- `Default(payload)` -- assign, narrowing-checked (the bare `key`).
- `Assign(payload)` -- assign without the check (`key__assign`).
- `Extend(payload)` -- merge onto the layer below (`key__extend`).

The load-bearing invariant is that a node's payload is **never** a bare node -- it is a
leaf or a nested `Patch`. The algebra inspects and rewrites only the *outermost* wrapper
and never re-parses a key string, which is what makes a stacked suffix like
`a__extend__assign` harmless: it lifts to a literal field name `a__extend` under a single
`Assign` wrapper and is never re-interpreted.

## API

All operations are pure functions over a `Patch`.

| Function | Purpose |
| --- | --- |
| `lift(raw)` | Suffix-keyed surface dict -> `Patch`. Resolves within-layer "reset then add"; raises on the bare-plus-`__assign` conflict. A plain, already-resolved dict (no operators) lifts to an all-`Default` `Patch`, so use `lift` for a concrete base too. |
| `merge(lower, higher)` | Combine `higher` over `lower`; **raises `NarrowingError`** (aggregating every narrowing path) -- the strict default. |
| `merge_narrowing_allowed(lower, higher)` | Same combine, but returns `(patch, narrowing_paths)` for the caller to surface or discard instead of raising. |
| `finalize(patch)` | Collapse a `Patch` to a plain, marker-free `dict` (a surviving `Extend` resolves against nothing = assign). |
| `lower(patch)` | Inverse of `lift`: a `Patch` back to a suffix-keyed dict, for carrying an unresolved patch as plain (e.g. JSON-able) data. |

`NarrowingError` and the base `OverlayError` live in `errors`; the node types and
markers in `nodes` / `markers`.

### Typical pipeline

`lift` each layer, fold them low -> high with `merge` (or `merge_narrowing_allowed` if
you want to decide on narrowings yourself), then `finalize` once:

```python
patch = lift(defaults_layer)
for layer in (user_layer, project_layer, local_layer):
    patch = merge(patch, lift(layer))   # or merge_narrowing_allowed
result = finalize(patch)                # plain dict; re-parse into your own types
```

If you have a concrete runtime base, `lift` it and put it at the **bottom** of the fold so a
higher `Extend` extends it and a higher `Default` replaces (and is narrowing-checked)
against it. A base with no operators lifts to all-`Default`; a stray `__extend` in it is
honored (extend-against-nothing = the value).

### Deferred resolution against a runtime base

`merge` is **associative**, so the base does not have to be present when you combine the
static layers. If part of the base is only built at runtime, combine the static layers at
load time, `lower` the combined patch back to a suffix-keyed dict to carry it as plain
data, then `lift` it again and `merge` it onto the runtime base once that base exists.
The result is identical to having merged everything in one pass -- a surviving `__extend`
simply waits for its base and resolves when one finally appears.

## Properties

- `merge` is **pure, deterministic, and associative**.
- Within-layer resolution is **order-independent** (safe for unordered sources).
- **Dependency-free** (standard library only).
- **Total**: no hooks, callbacks, or policy parameters. Every behavior is expressed in
  the operator language, which makes the algebra trivially testable in isolation.

## What it does NOT do (the consumer's job)

- Parse files / env / CLI into dicts.
- Own your schema or types -- serialize your config object to a dict *before* and
  re-parse *after*; your own type system re-coerces declared types.
- Decide which fields behave specially -- that is encoded as suffixes/markers by your own
  pre-processing.
- Decide when to surface narrowing errors -- the library reports, you raise.
