# `overlay` — a layered config-merge algebra (proposed; working name TBD)

> **Status: proposed / future work.** This is a captured design + draft README for a library
> we intend to extract from mngr's config layer **after** the `__assign` + `merge`/`finalize`
> refactor lands (that refactor stabilizes the algebra; extracting before it would mean
> extracting a moving target). It records the context from the design discussion so it isn't
> lost. Nothing here is implemented as a standalone library yet; the seed code lives in
> `libs/mngr/imbue/mngr/config/key_resolver_primitives.py` (+ `fold_settings_patch` and the
> dict-level narrowing predicate in `data_types.py`).

## What this is

A small, **total, dependency-free** library for merging **layered configuration**. It
operates purely on plain dicts / lists / sets / scalars plus a tiny set of key-suffix
operators. It knows nothing about your config framework (pydantic, dataclasses), your file
formats (TOML, YAML, env vars, CLI flags), or your schema. You compile your config into its
operator language, it merges, you read the result back and re-parse into your own types.

It owns exactly one thing: **the algebra of how N precedence-ordered layers of config
combine** — assign vs. merge, narrowing detection, and deferred resolution against a runtime
base. Nothing else.

## Why it exists

Layered config (built-in defaults < user < project < local < env < CLI) needs a *precise,
predictable* merge. The two obvious defaults are both surprising:

- **Assign-by-default**: a higher layer's value replaces the lower's *wholesale* — so setting
  one nested key silently drops its siblings.
- **Deep-merge-by-default**: you can never cleanly *replace* a value, and list semantics are
  ambiguous.

The fix is **explicit operators** (assign vs. extend) plus a **narrowing guard** that flags
when an assign silently drops entries from the layer below. That combination is generic and
reusable across any layered-config system — hence a standalone library.

## Core model

### Values
Ordinary JSON-shaped data: `dict`, `list`, `set`, scalar. Plus optional **`Static*` wrappers**
(`StaticTuple`, `StaticList`, `StaticDict`): "this aggregate is **atomic** — replacing it is a
value-set, not narrowing." (mngr's `StringDerivedTuple` — a TOML string like
`cli_args = "--a --b"` coerced into a tuple — becomes a `StaticTuple` subclass.)

### Operators (key suffixes)
- **bare `key`** → **assign**: replace the value from the layer below. Narrowing-checked.
- **`key__extend`** → **merge** onto the layer below: list concat, set union, **recursive**
  dict merge (nested `__extend` go deeper; nested *bare* keys assign at their level). Never
  narrows — an extend is always a superset.
- **`key__assign`** → **assign without the narrowing warning**: an explicit "yes, I am
  replacing this, I know it drops things."

### Within-layer resolution (order-independent)
A single layer is resolved in two phases: **assign-phase** (bare keys and `__assign`) then
**extend-phase** (`__extend`). This is deliberately **independent of key order** — important
because some sources are unordered (env vars have no document order). Exactly one combination
is an error: bare `key` **and** `key__assign` for the same key in the same layer (two
contradictory assigns — pick one). Everything else falls out mechanically:
- `key` + `key__extend` → assign then extend = "reset, then add".
- `key__assign` + `key__extend` → no-warn assign then extend = "reset-without-warning, then add".

### Narrowing
A **bare** assign that drops a non-empty aggregate entry from the layer below is a *narrowing*,
recorded with its dotted path — **recursively**, including bare keys nested inside an
`__extend` value. `__extend` never narrows (superset); `__assign` and `Static*` values suppress
it. The library only **reports** narrowings; **the caller decides when and whether to raise.**
Crucially there is **no global "allow narrowing" flag** — suppression is *per-key* via
`__assign`, which is what lets a caller raise *immediately* on an unexpected narrowing instead
of aggregating violations and deferring the decision until a global flag's final value is
known.

## Operations

### `merge(lower, higher) -> (patch, narrowings)`
Combine two patches, `higher` over `lower`. **Preserves** any `__extend` marker that has
nothing concrete to resolve against (so it can resolve later against a runtime base);
**combines** two markers for the same key; a higher **bare** key wins over a lower marker.
Records narrowings. **Pure. Never raises. Associative** —
`merge(merge(a, b), c) == merge(a, merge(b, c))` — so layers can be combined in any grouping,
and a runtime base supplied early or late gives the same result.

### `finalize(patch) -> dict`
Resolve any **remaining** `__extend` against nothing (extend-against-empty = assign),
producing a **marker-free** dict. Pure. (No assertion step — if a marker survived because a
base was never supplied, that surfaces as missing base keys, which ordinary tests catch; the
finalize *resolving* the marker is the correct behavior, not a bug to assert against.)

### Pipeline
Fold your layers low → high with `merge` (put a concrete runtime base at the **bottom** if you
have one), then `finalize` once, and raise on the accumulated `narrowings` per your policy
(typically: immediately, since suppression is per-key). Associativity means you can pre-combine
the layers available at config-load time and `merge` a later-arriving runtime base onto the
front — identical result.

## The "no dependency injection" principle

The library has **zero hooks, callbacks, or policy parameters.** It is a *total language*:
every behavior is expressed as operators/markers in the dict you hand it. You encode your
policy two ways — by **compiling your config into the operator language** (a dict→dict
pre-processing pass) and by **choosing when to `merge` vs. `finalize`.** Worked policies (all
from mngr):

- **Deep structural merge** (a container of named sub-configs that should merge per-key,
  preserving untouched fields — e.g. `agent_types`, `providers`): recursively rewrite every
  **dict-valued** key in that subtree to `key__extend` (leaves stay bare). Recursive
  `__extend` then deep-merges it, leaf-assigns, and preserves untouched siblings — exactly the
  container semantics, with no "container" concept in the library.
- **A field that accumulates across layers** (e.g. Claude `settings_overrides`): rewrite it to
  `__extend` (or let users write `__extend`). The old `SettingsPatchField` pydantic annotation
  becomes "mark this field `__extend` during pre-processing."
- **Defer a field's resolution to a runtime base that doesn't exist yet**: keep it as a patch
  (`merge` only), and `finalize` it against the base *later* — don't finalize it early. The old
  "deferred-path registry" becomes "the consumer chooses what to finalize when," not lib
  config.
- **Atomic aggregate values**: wrap in `Static*`.
- **Per-key narrowing opt-out**: emit `key__assign`. (Replaces the old global
  `allow_settings_key_assignment_narrowing` flag.)

## Pre-processing toolkit (shipped helpers, still no DI)

Because every policy is a `dict→dict` transform, the library can ship a small toolkit of
**pure** helper transforms so consumers don't hand-roll the common ones. These are *not*
dependency injection — the core `merge`/`finalize` take no hooks; the helpers just produce the
operator-language dict you then pass in. Likely helpers:

- `mark_subtree_extend(d, path)` — recursively rewrite every **dict-valued** key under `path`
  to `key__extend` (leaves untouched). The container-deep-merge transform.
- `mark_path_extend(d, path)` — mark a target field **and all of its ancestors** `__extend`,
  so a specific nested field merges into the layer below instead of being wiped by a bare
  ancestor. (You need the whole chain from the root down marked, or an intermediate bare level
  replaces everything beneath it.)
- `as_static(value)` / `StaticTuple(...)` etc. — wrap an atomic aggregate so replacing it is a
  value-set, not narrowing.
- `mark_assign(d, path)` — emit `key__assign` to opt a key out of the narrowing warning.

A consumer's pre-processing then reads like a short pipeline of these — e.g. mngr:
`mark_subtree_extend(cfg, "agent_types")`, `mark_subtree_extend(cfg, "providers")`,
`mark_path_extend(cfg, "agent_types.<name>.settings_overrides")`, wrap string-tuples as
`StaticTuple` — all pure, all composable, none of it reaching into the merge core.

## What the library does NOT do (the consumer's job)

- Parse files / env / CLI into dicts.
- Own your schema or types. Serialize your config object to a dict *before* and re-parse
  *after* — your type system re-coerces declared types (tuple vs. list, etc.), so the merge
  itself stays type-agnostic. (This is why pre-runtime field-type awareness isn't needed.)
- Decide which fields behave specially — that's encoded as markers via pre-processing.
- Decide when to surface narrowing errors — the lib reports; you raise.

## Reference consumer: mngr

mngr's flow would be: parse each layer (TOML / env / `--setting`) into a dict; **serialize**
the accumulating config object to a *config-shaped* dict; **pre-process** (recursively mark
container subtrees `__extend`; mark `settings_overrides` `__extend`; wrap string-derived
tuples as `StaticTuple`); **`merge`** the layers; **`finalize`** (against the provision base
`B` for deferred fields like `settings_overrides`); **re-parse** into `MngrConfig`.

Residual wrinkle to design for: `CommandDefaults` / `CreateTemplate` stash arbitrary keys in
an inner `.defaults` / `.options` dict, so a naive `model_dump` yields `commands.<cmd>.defaults.<k>`
while the config path is `commands.<cmd>.<k>`. A "config-shaped" serialization must flatten
those transparent wrappers so override paths line up with base paths (this is exactly what
mngr's current `_walk_to_field` special-case compensates for).

## Properties

- `merge` is **pure, deterministic, associative**.
- Within-layer resolution is **order-independent** (safe for unordered sources).
- **Dependency-free** (stdlib; at most a `pure`-style decorator).
- **Total**: no escape hatches, hooks, or policy parameters — all behavior lives in the
  operator language, which makes it trivially testable in isolation (property-test the
  associativity/narrowing contracts directly).

## Extraction plan (when the time comes)

1. Land `__assign` + the `merge`/`finalize` unification in mngr first (collapses
   `combine_patches` + `fold_settings_patch` into one `merge` + `finalize`, threads narrowing
   in, drops the aggregation machinery and the global flag).
2. Lift `key_resolver_primitives.py` + `merge`/`finalize` + the dict-level narrowing predicate
   + the `Static*` markers into the library; give it its own error type and (optional)
   `pure` shim.
3. Convert mngr's remaining policy into pre-processing passes (container marking,
   `settings_overrides`, `StringDerivedTuple` → `StaticTuple`) and a config-shaped
   serialize/re-parse around the lib call.
4. Leave pydantic-model resolution, schema, and file/env parsing in mngr. The library stays a
   dict→dict algebra.

### Status and what gates step 3

Steps 1-2 are **done**: the library exists (`libs/overlay`), `__assign` / `merge` / `finalize`
landed, and the typed-node algebra (`Default`/`Assign`/`Extend`; see
[overlay-typed-nodes.md](./overlay-typed-nodes.md)) is the enabling core. Today mngr routes only
a few things through overlay -- `resolve_extends` (raw-dict `__extend` resolution against the
model base), the `settings_overrides` `SettingsPatchField` combine, and the provision-time
`_build_settings_json` fold. The *rest* of config merging (cross-scope `merge_with`, `parent_type`
inheritance) is still pydantic-model field-by-field assignment that `model_dump`s only to read
values; it does **not** round-trip the whole config through overlay.

Step 3 -- routing the **whole** config merge through overlay (serialize → pre-process → merge →
re-parse) -- is the remaining work. Nothing is a hard technical blocker, but it is gated on:

- **The narrowing-policy decision (Stage 2 in [config-merge-operators.md](./config-merge-operators.md)).**
  The unified path runs every field through overlay's narrowing, so the flag / raise-immediately /
  aggregate / `__assign` policy must be settled first or the integration is built on shifting ground.
- **`model_fields_set` fidelity (the subtle correctness risk).** The model-level merge applies only
  *explicitly-set* fields so an untouched field is not clobbered. A dict round-trip must preserve this
  via `model_dump(exclude_unset=True)`, reproducing the loader's nuanced None-handling ("parse_config
  sets every kwarg, often to None, so `model_fields_set` over-reports"). This is the trickiest part.
- **A config-shaped serializer that flattens transparent wrappers** (`CommandDefaults.defaults` /
  `CreateTemplate.options`) so override paths line up with base paths -- what `_walk_to_field`
  special-cases today.
- **Writing the pre-processing passes** (`mark_subtree_extend` for container fields,
  `settings_overrides`, re-wrapping `StringDerivedTuple`/`Static*`), since those markers/semantics do
  not survive `model_dump`.
- **Blast radius**: it rewrites the core config-load path everything depends on -- gated on appetite,
  not feasibility.

When this lands, the `merge_with` / `_apply_custom_overrides_to_parent_config` duplication (noted in
config-merge-operators.md) disappears wholesale rather than being DRY'd into an mngr-level helper.
