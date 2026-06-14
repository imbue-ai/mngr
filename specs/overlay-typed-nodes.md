# Overlay typed nodes: the operator algebra without string parsing

Status: **design (approved in principle, not yet implemented).** Pins the typed-node
representation that replaces the string-suffix internal representation in the `overlay`
library (`libs/overlay`). The surface syntax (`key__extend` / `key__assign` in TOML, env
vars, `--setting`, `mngr config`) is **unchanged**; a single *lift* pass at the config-load
boundary turns suffix-keyed dicts into node-valued patches, and the merge algebra works on
nodes from then on. Supersedes the "Future direction" note in
[config-merge-operators.md](./config-merge-operators.md); builds on the narrowing /
`Static*` semantics defined there.

## Why typed nodes

The string-suffix representation parses operators out of key strings (`bare_key`,
`is_extend_key`, ...) at every stage of the algebra (`extend_dict`, `combine_patches`,
`finalize`). That re-parsing is what makes **stacked suffixes** unsafe: a key like
`a__extend__assign` has exactly one suffix peeled per pass, so the inner `__extend` leaks
into the "resolved" dict as a literal key and then *reactivates* on a later `finalize`
(`{"a__extend__assign": v}` → `{"a__extend": v}` → `{"a": v}` with the extend firing). An
operator the user never intended gets reinterpreted as one.

Typed nodes remove the re-parsing entirely. The operator lives in the **type** of the
value, not in the key string. The algebra dispatches on the node type and may **rewrite the
wrapper**, but it **never unwraps a payload to look for an inner operator**. So:

- `Extend(Assign(x))` is never produced by the lift (one wrapper per dict-value), and even
  if hand-constructed it is harmless: `combine`/`finalize` see the outer `Extend`, treat
  `Assign(x)` as opaque payload data, and never interpret the inner `Assign`.
- This is exactly why **`Default` is a real wrapper, not "raw values mean assign."** Making
  every operator-level dict-value uniformly a node means the algebra never has to ask "is
  this payload an operator or data?" — the answer is always *data* (a leaf, or a nested
  patch of nodes).

## Core types

```
Patch    = dict[str, Node]
Node     = Default(payload) | Assign(payload) | Extend(payload)
payload  = Leaf | Patch
Leaf     = scalar | list | tuple | set | frozenset      # incl. Static* leaf subclasses
```

**Invariant (load-bearing):** a payload is *never* a bare `Node`. It is either a leaf or a
nested `Patch` (a `dict[str, Node]`). The lift establishes this; `combine`/`finalize`
preserve it. The algebra inspects and rewrites the *outermost* wrapper of each dict-value
only; payloads are opaque to operator interpretation (narrowing analysis may *read* a
payload's aggregate contents, but that is data inspection, never operator parsing).

Nodes are immutable typed wrappers (frozen dataclasses; the library stays dependency-free,
so no pydantic). They carry a single `payload` (alias `value`). They are compared by
type+payload; they are not hashed.

### Node semantics

- **`Default(p)`** — assign, replacing the layer below. **Narrowing-checked**: records a
  violation if it drops a non-empty aggregate (unless `p` is a `Static*` leaf). This is the
  bare-`key` behavior.
- **`Assign(p)`** — assign, replacing the layer below, **without** the narrowing check
  (the explicit "I am replacing this, don't warn"). This is `key__assign`.
- **`Extend(p)`** — merge onto the layer below (list concat, set union, recursive patch
  merge). **Never narrows** (an extend is a superset). This is `key__extend`.
- `Static*` (`StaticTuple` / `StaticList` / `StaticDict` and the `ScalarTuple` /
  `StringDerivedTuple` subclasses) are **leaf payloads**, not nodes: a `Default(StaticTuple(...))`
  is a narrowing-*exempt* assign. They mark "this aggregate is atomic; replacing it is a
  value-set, not narrowing."

## The lift (surface syntax → nodes)

`lift(raw: dict[str, Any]) -> Patch` runs once, at the config-load boundary (mngr side),
turning a suffix-keyed dict into a node-valued patch. For each **bare field name** in
`raw`, gather its forms:

- a bare key (→ `Default`), and/or
- a `name__assign` key (→ `Assign`), and/or
- a `name__extend` key (→ `Extend`).

A value `v` is payload-lifted recursively: a `dict` becomes a nested `Patch` (`lift(v)`); any
other value is a leaf, preserved as-is (`Static*` leaves included).

Rules per bare name:

- **bare and `name__assign` together → error** (`OverlayError`): two contradictory assigns
  of the same field in one layer (replaces the old `check_no_conflicting_assign`).
- **assign form only** → `Default(lift v)` (bare) or `Assign(lift v)` (`__assign`).
- **extend form only** → `Extend(lift v)`.
- **assign form and extend form together** (the within-layer "reset, then add" idiom): the
  assign establishes the value and the extend applies on top, producing **one** node of the
  assign's kind whose payload is the extend applied onto the assign's payload:
  `Default(apply_extend(v_assign, v_extend))` (or `Assign(...)`). This is the *only* place
  the lift combines; it is what the old two-phase within-layer resolution did, now done once
  up front so each layer is a clean one-node-per-key patch. (Order-independent: it does not
  matter which suffix key appears first in the source dict — important for unordered sources
  like env vars.)

Because the lift only ever strips operator suffixes from the *outermost* key once and never
re-parses, a stray `a__extend__assign` lifts to field name `a__extend` with an `Assign`
wrapper; the `a__extend` is a literal name forever and never reactivates. The lift **may**
additionally reject a bare name that still ends in an operator suffix (a clearer error than a
silently-literal weird field); this is optional hygiene, not a safety requirement — the
node model is safe regardless.

## `combine(lower: Patch, higher: Patch) -> (Patch, list[AssignDrop])`

Cross-layer combine, `higher` over `lower`, pure/recursive/associative. Per key:

- key in one side only → carried through unchanged.
- key in both → `combine_nodes(lower[k], higher[k])`:
  - **higher is `Default` or `Assign`** (assign-kind): higher **wins wholesale** — result is
    the higher node unchanged; the lower node is dropped. If higher is `Default`, append an
    `AssignDrop` candidate `(lower_payload, higher_payload, path)` for the narrowing filter;
    if `Assign`, append nothing. (Wholesale replace matches today's bare-dict semantics: a
    `Default` dict drops lower keys it does not mention — which is precisely what narrowing
    flags.)
  - **higher is `Extend`**:
    - lower is `Default(Lp)` / `Assign(Lp)`: result = **same kind as lower** with payload
      `apply_extend(Lp, higher.payload)` — extend the assigned value; the wrapper stays the
      lower's kind. Never narrows.
    - lower is `Extend(Lp)`: result = `Extend(combine_extend_payloads(Lp, higher.payload))`
      — both unresolved, combine their payloads (patch→recursive `combine`; list→concat;
      set→union). Stays `Extend` (still deferred — no base yet).

`AssignDrop = (lower_payload, higher_payload, dotted_path)`. Narrowing is recorded **only**
when a `Default` replaces an existing lower assign-kind node — never when the lower node is
an `Extend` (its increment is not a base to narrow), matching today's behavior.

`apply_extend` / `combine_extend_payloads` operate on payloads (leaf or patch). A patch
payload recurses through `combine`; a leaf payload does list-concat / set-union; a scalar
target is an error (`OverlayError`); extend-against-absent acts as assign.

## `finalize(patch: Patch) -> dict[str, Any]`

Collapse every node to a plain value (no nodes remain):

- `Default(p)` / `Assign(p)` → `finalize_payload(p)`.
- `Extend(p)` → `finalize_payload(p)` (extend-against-nothing = assign).

`finalize_payload(p)` is `finalize(p)` when `p` is a `Patch`, else the leaf `p`. Pure; no
assertion (a surviving `Extend` collapsing to an assign is the correct "nothing to extend
against" outcome).

## Merging against a concrete base

A concrete base `B` (plain dict, e.g. the provision settings base, or a `create` command's
params, or a lower config layer already reduced to plain values) is merged with a patch by
**lifting `B` to an all-`Default` patch** (its values are already-set assigns), combining,
then finalizing. This unifies "merge two patches" and "resolve a patch against a base": a
higher `Extend` over `B`'s `Default(B_value)` extends `B_value`; a higher `Default` over it
replaces and narrowing-checks. Deferred subtrees (mngr's `create_templates` /
`settings_overrides`) keep their `Extend` nodes unresolved until their runtime base exists,
then merge against it the same way.

## The public narrowing API

The narrowing predicate `would_assignment_narrow(base_value, override_value)` stays a
plain-value function (it compares **finalized** payloads; `Static*` and superset overrides
are exempt). The filter turns `AssignDrop` candidates into dotted narrowing paths:
`would_assignment_narrow(finalize_payload(lower_payload), finalize_payload(higher_payload))`.

Two explicit public functions over one private core (`_merge` does combine + collect, never
raises):

- **`merge(lower, higher) -> Patch`** — raises `NarrowingError(paths)` aggregating **all**
  narrowing paths from this combine, if any. The strict default: callers that want the
  safety net call this and get every violation at once.
- **`merge_narrowing_allowed(lower, higher) -> (Patch, list[str])`** — never raises; returns
  the combined patch **and** the narrowing paths, which the caller may surface or discard.

This replaces the global `allow_settings_key_assignment_narrowing` flag with an explicit
call-site choice (and complements the per-key `Assign` opt-out). A caller that folds N
layers and wants *one* aggregated error at the end uses `merge_narrowing_allowed` per layer,
accumulates the paths, and raises its own `NarrowingError` once — which is what mngr's loader
does today via `_collect_layer_narrowing`. `NarrowingError` is an `OverlayError` subclass
carrying the list of paths.

## mngr integration (consumer side): the engine swap

mngr keeps its **suffix-keyed dict** as the storage / boundary representation everywhere
(TOML, env, `--setting`, `mngr config`, and the stored `settings_overrides` /
`create_templates.options` dicts -- all plain JSON-able data, no node objects in pydantic).
The node algebra is the **internal engine**, bridged at the boundaries by `lift` (suffix dict
-> node patch) and `lower` (node patch -> suffix dict). This is deliberate: an investigation
confirmed mngr never serializes these dicts after load, but the plain-data contract is
simpler and lower-risk than threading node objects through the schema, and the leak is fixed
either way.

**Why the leak is still fixed under the engine swap.** Within any single resolution pass,
`lift` runs **once** (it strips only the outermost suffix into a node; an inner suffix stays a
literal field-name character and is never re-parsed). `finalize` is only ever the **terminal**
step. Deferred markers are carried across passes via `lower` (which re-emits each node's own
suffix faithfully: `Assign` -> `key__assign`, `Extend` -> `key__extend`, `Default` -> bare),
so a later `lift` reproduces the same nodes and never reactivates a stray suffix. (The old
string algebra reactivated precisely at the terminal `finalize` on a stacked suffix; the node
algebra lowers/re-lifts faithfully instead, so a stacked suffix at worst yields a harmless
literal field name in the terminal output.) No stacked-suffix guard is needed.

Touch points:

- **`resolve_extends` (3 call sites: loader, `--setting`, `mngr config`)** becomes node-based:
  `lift` the override, walk the pydantic/dict base (`_walk_to_field`, unchanged --
  `CommandDefaults` / `CreateTemplate` transparent wrappers), resolve `Extend` nodes against
  the base and `finalize` the non-deferred parts to plain values; on **deferred paths**
  (`is_deferred_extend_path`) `lower` the surviving nodes back to suffix strings so provision
  re-lifts them. The deferred-path registry/matchers stay in mngr (consumer policy).
- **Deferred `__assign` fix.** Because deferred subtrees are now carried via `lower`, a
  deferred `key__assign` survives as `key__assign` (re-lifted to an `Assign` at provision,
  honoring the no-warn intent) instead of being collapsed to a narrowing-checked bare assign.
  This corrects a latent inconsistency in the string-suffix path; it is a small, intentional
  behavior change (tested).
- **`SettingsPatchField` combine (2 sites: `merge_with`, `_apply_custom_overrides_to_parent_config`)**
  combines two stored `settings_overrides` suffix dicts: `lift` both, `combine` (node patches),
  `lower` back to a suffix dict for storage. The marker stays a mngr pydantic annotation.
- **`detect_settings_narrowing` / `_walk_for_narrowing` / `_check_narrowing` stay in mngr**
  (they walk pydantic models at config-load) and keep calling the overlay value-level
  `would_assignment_narrow` at the leaves. The config-load narrowing aggregation
  (`_collect_layer_narrowing`) and the `allow_settings_key_assignment_narrowing` flag are
  unchanged.
- **`_build_settings_json` (mngr_claude)** merges the stored `settings_overrides` suffix dict
  onto the provision base `B`: `lift` the overrides, `lift_concrete(B)`, then `merge`
  (or `merge_narrowing_allowed` when the flag allows it), and `finalize` to a plain dict.
- **The flag stays for now.** Removing `allow_settings_key_assignment_narrowing` and switching
  to raise-immediately remains the deferred Stage 2 decision (see config-merge-operators.md);
  the new API supports both policies, so nothing forces that decision here.

## What does NOT change

- Surface syntax: users still write `key__extend` / `key__assign`.
- `Static*` markers and the narrowing exemptions.
- The associativity contract: `finalize(merge(merge(B, X), Y)) == finalize(merge(B,
  merge(X, Y)))` (now over node patches) — still property-tested.
- mngr's pydantic schema, file/env parsing, and deferred-path policy.

## Build phases

1. **overlay lib (isolated):** add the `Default` / `Assign` / `Extend` node types, `lift`,
   the node `combine` / `finalize` / `apply_extend`, node-aware `_merge` + public `merge` /
   `merge_narrowing_allowed`, `NarrowingError`. Rewrite the overlay tests (operators →
   lift, merge → node merge, associativity over nodes). 100% coverage, green in isolation.
2. **mngr rewiring:** lift at the parse boundaries; node-aware `resolve_extends`; deferred
   `Extend` preservation; `SettingsPatchField` combine over nodes; loader / narrowing wired
   to the new API; `_build_settings_json` updated. Keep behavior identical (the flag still
   gates raising). Update mngr tests, ratchets, changelogs.
3. **cleanup:** drop the now-unused string-suffix internals (`is_extend_key`, `bare_key`,
   `extend_dict`, the suffix-based `combine_patches`, `check_no_conflicting_assign`) once no
   caller remains; keep only the surface-suffix constants the lift needs.
```
