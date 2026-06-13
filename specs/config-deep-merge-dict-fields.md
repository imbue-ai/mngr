# Recursive (deep) `__extend` for config dict fields

Status: **CONCLUDED -- no change needed (kept for the reasoning).** After review, the
decision is to **keep `__extend` one-level** (consistent with mngr's deliberately
non-recursive operator): a user places `__extend` at the level they want merged
(`permissions__extend`), and the existing one-level shallow merge already preserves siblings
one level down -- which covers #1647 and Claude's essentially 2-level settings. Deep
recursion is unnecessary and would depart from the documented semantics. The
`settings_overrides` work (see [claude-settings-overrides](./claude-settings-overrides.md))
therefore needs **no** change to `libs/mngr`'s `__extend`. The recursive design below is
retained only as a record of what was considered and why it was rejected.

Audience: developers working in `libs/mngr/imbue/mngr/config/`.

> **OPEN -- is this change even needed?** mngr's existing dict `__extend` is a **one-level
> shallow** merge (`{**current, **extend}`), and one level **already preserves siblings one
> level down**. So #1647's case is solved by placing `__extend` at the right level --
> `settings_overrides.permissions__extend = {allow = [...]}` keeps a sibling
> `permissions.defaultMode` via the shallow merge, no recursion required. Deep recursion is
> only needed when `__extend` is placed *too shallow* to reach the level you want merged
> (e.g. `settings_overrides__extend = {permissions = {...}}` replaces all of `permissions`).
> Since the user can always put `__extend` at the level they want merged, and Claude's
> settings are essentially 2-level (a top key -> a flat dict of scalars/lists), one-level
> `__extend` likely suffices -- and it stays consistent with mngr's deliberately
> **non-recursive** `__extend` (env-settings-overrides line 43: "No recursion into nested
> aggregates"). **Leaning: do NOT make `__extend` recursive.** Keep it one-level; users place
> `__extend` at the level they want merged. The remainder of this spec describes the
> recursive alternative for completeness, pending a decision.

## The change in one line (recursive alternative)

`_apply_extend`'s dict branch (`key_resolver.py`) currently does `{**current, **extend}`
(shallow, one level). The recursive alternative: when `__extend` merges two dicts, a nested
value that is **itself a dict in both** is merged recursively rather than the inner dict being
replaced wholesale; nested lists concat, nested sets union, leaves (scalar / type-mismatch)
take the extender's value. This is gated entirely on `__extend` -- a bare assignment still
replaces. Everything else -- assign-by-default, narrowing, `resolve_extends`' cross-layer
behavior -- stays as is.

**Note (per review):** this is NOT "nested dicts always merge key-by-key." Merging happens
only under an `__extend`; without it, a dict assignment replaces. The question this spec
turns on is whether a *single* `__extend` should recurse through all nested levels
(recursive) or merge just one level and require a nested `__extend` to go deeper (the current
one-level behavior -- the leaning above).

## Background

- **`__extend` is shallow for dicts today.** `_apply_extend` (`key_resolver.py`,
  dict branch) returns `{**current_value, **extend_value}` -- a one-level key merge. So
  `field__extend` preserves *top-level* keys, but a re-specified nested dict (e.g.
  `permissions`) replaces its inner siblings wholesale. This shallowness is exactly what
  blocked adopting the mngr scheme in #1647 (a `permissions.allow`-only override dropped the
  sibling `permissions.defaultMode`). The
  [env-settings-overrides](./env-settings-overrides/concise.md) spec documents this shallow
  behavior deliberately (line 43); this change supersedes that.
- **Narrowing already handles deep supersets.** `would_assignment_narrow`
  (`data_types.py`) recurses for dicts and returns "narrowing" only if the override **drops
  a base key** at some level; a superset passes. A deep `__extend` result is a superset at
  every level, so it is **already** narrowing-exempt -- no change to the guard. A bare
  assign that drops keys still narrows (hard error), which is the desired "warn unless you
  used `__extend`" behavior.
- **`__extend` resolves cross-layer.** `resolve_extends` runs per layer against the
  accumulated `base_config`, so `field__extend` in project scope merges onto the
  already-merged user scope. With a deep dict merge, this gives deep **cross-scope**
  condensation (user < project < local) with no change to `merge_with` (which stays
  assign-by-default).
- **Native Claude deep-merges its own layers (verified).** A sibling `env` key in a project
  `.claude/settings.json` survives a different sibling set via `--settings`. So the on-disk
  side of #1647 is handled by Claude itself; this spec covers only mngr's config-scope merge.

## Expected behavior

### Deep `__extend` merge semantics

Define `_deep_extend_merge(base, override)` and call it from `_apply_extend`'s dict branch:

- **dict + dict** -> recurse per key. Keys only in `base` are **preserved**; keys only in
  `override` are added; shared keys recurse.
- **list + list** -> concatenate (matching `__extend`'s existing list behavior).
- **set + set** -> union (matching `__extend`'s existing set behavior).
- **leaf** (scalar, or mismatched aggregate types, e.g. base dict vs override scalar) ->
  the **override value wins** (replaces). Note: this is leaf-replacement *inside* a
  `__extend`, distinct from the top-level rule that `field__extend` on a whole **scalar
  field** is an error (that top-level contract in `_apply_extend` stays).
- Pure, no input mutation.

This is just `__extend`'s existing per-type rules applied **recursively** at every depth,
instead of stopping after the first level.

### What does NOT change

- **Assign-by-default.** A bare `field = {...}` still replaces the base value. If it drops a
  non-empty base key, the **narrowing guard still hard-errors** (unless
  `allow_settings_key_assignment_narrowing`). This is the behavior the user explicitly wants:
  "warn if someone sets `some_key = {c: 2}` over `{a: 0, b: 1}` rather than
  `some_key__extend = {c: 2}`."
- **The narrowing guard.** No exemptions added; it already passes deep supersets and flags
  dropped keys (recursively).
- **`merge_with`.** Stays assign-by-default for every field. No field annotation/marker, no
  per-field merge policy. `settings_overrides` needs **no** special-casing in core.

### Worked example (cross-scope, via `__extend`)

```toml
# user settings.toml
[agent_types.coder.settings_overrides.permissions]
defaultMode = "auto"

# project settings.toml  -- note the __extend
[agent_types.coder.settings_overrides__extend.permissions]
allow = ["Bash(npm *)"]
```
Resolved `coder.settings_overrides`:
`{"permissions": {"defaultMode": "auto", "allow": ["Bash(npm *)"]}}` -- both siblings
survive. Today (shallow `__extend`): `{"permissions": {"allow": [...]}}` -- `defaultMode`
lost (the #1647 bug). A bare `settings_overrides.permissions = {allow=[...]}` at project
scope still narrows (drops `defaultMode`) -> hard error telling the user to use `__extend`.

(Exact TOML spelling of `field__extend` on a nested table is per the existing
env-settings-overrides surface; the point is the operator now merges deep.)

## Changes

`libs/mngr/imbue/mngr/config/key_resolver.py`:

- Add `_deep_extend_merge(base, override)` (pure; dict recurse, list concat, set union, leaf
  override-wins).
- `_apply_extend` dict branch: replace `{**current_value, **extend_value}` with
  `_deep_extend_merge(current_value, extend_value)`. The list/set/scalar top-level branches
  are unchanged.

`libs/mngr_claude/imbue/mngr_claude/plugin.py`:

- No code change required for the merge itself. Update `settings_overrides`' field
  description to note it deep-merges across config layers via `settings_overrides__extend`
  (and that a bare re-assign replaces, with the usual narrowing warning).

Docs / changelog:

- Update [env-settings-overrides](./env-settings-overrides/concise.md) line 43 (dict
  `__extend` is now deep, not shallow). Update `libs/mngr/docs/concepts/environment_variables.md`
  / config docs accordingly.
- Changelog: `__extend` on dict fields now merges recursively; nested sibling keys are
  preserved. Call out as a **behavior change** to the prior shallow semantics.

## Open questions

1. **Global vs scoped.** This makes dict `__extend` deep for **all** fields (uniform --
   "the same way the rest of our settings merge"). That is a behavior change to the
   documented shallow semantics and flips the existing
   `test_resolve_extends_shallow_merges_dict_field` to assert deep. Confirm global is wanted
   (recommended, for uniformity) vs scoping deep-merge to only certain fields (which would
   reintroduce a marker and is more complex). Default: **global**.
2. **Leaf type mismatch.** When a nested key is a dict in the base but a scalar/list in the
   override (or vice versa), this spec says **override wins** (replace). Alternative: raise a
   `ConfigParseError` (stricter, catches likely mistakes). Default: override-wins for
   simplicity; revisit if it masks errors.
3. **Replacing a nested subtree.** Deep `__extend` removes the old shallow trick of
   "replace just this nested key by re-specifying it under `__extend`." To replace a nested
   subtree now, the user re-assigns the parent (bare) and accepts the narrowing
   warning/escape, or restructures. Confirm this loss is acceptable (it matches "bare =
   replace, `__extend` = merge").

## Tests

`libs/mngr/imbue/mngr/config/key_resolver_test.py`:

- Flip `test_resolve_extends_shallow_merges_dict_field` to deep: a nested sibling survives a
  nested `__extend` (was the asserted shallow drop).
- Deep `__extend`: 3-level nested dict merge preserves siblings; nested list concat; nested
  set union; nested scalar override; leaf type-mismatch override-wins; no input mutation.
- Cross-layer: `field__extend` in a higher layer deep-merges onto the lower layer's value.

`libs/mngr/imbue/mngr/config/data_types_test.py`:

- Narrowing unchanged: a deep-`__extend` superset does **not** narrow; a bare assign dropping
  a nested sibling still narrows.

`libs/mngr/imbue/mngr/config/loader_test.py`:

- End-to-end user/project/local `settings_overrides__extend` condenses nested siblings across
  all three scopes; bare re-assign at a higher scope raises the narrowing error.
