# Deferred `__extend` resolved against the provision base (settings_overrides)

Status: **spec only -- not yet implemented.** Makes the `settings_overrides`-onto-home
merge follow mngr's config-consistent semantics (bare = assign + narrowing guard;
`__extend` = merge) instead of the interim deep-merge-by-default. Generalizes the
`create_templates` deferred-`__extend` mechanism. Part of the `mngr/claude-hook-leak` PR.

Audience: developers in `libs/mngr/imbue/mngr/config/` and `libs/mngr_claude`.

## The model (one left fold)

Conceptually, the agent's effective Claude settings are a single left fold, lowest to
highest precedence:

```
merge( merge( merge( merge(B, U), P), L), <env>, <--setting> )
```

- `B` = the **provision base**: the synced `~/.claude/settings.json` (or generated defaults)
  + unattended flags + mngr's own hooks. Built at **provision** time.
- `U` / `P` / `L` / ... = the `settings_overrides` contributed by each mngr config scope
  (user / project / local / env / `--setting`).
- Each `merge(acc, X)` applies mngr's standard rules: a **bare** key in `X` **assigns**
  (replaces) the accumulator's value -- and if that drops a non-empty aggregate entry, the
  **narrowing guard** hard-errors (unless `allow_settings_key_assignment_narrowing`); a
  `key__extend` in `X` **extends** the accumulator's value (concat list / union set /
  one-level dict merge), narrowing-exempt.

There is exactly one accumulator and one base at each step, so the result is unambiguous.
`B` is just the bottom layer. This replaces `deep_merge_settings` (deep-by-default).

The wrinkle: `B` does not exist at config-load (it is built at provision), but `U/P/L` are
condensed at config-load by the normal config merge. So the fold is realized in two phases
that must equal the single fold:

1. **Config-load:** condense `U/P/L/...` into one `settings_overrides` value, **preserving**
   any `__extend` whose base is not present in a lower config scope (it is destined for `B`).
2. **Provision:** `merge(B, condensed_settings_overrides)` -- resolve the preserved
   `__extend` against `B` and apply the narrowing guard against `B`.

## Deferred-`__extend` mechanism (generalize `create_templates`)

Today `resolve_extends` (`key_resolver.py`) preserves an `__extend` verbatim (instead of
collapsing it to an assign) when `current is None and _is_create_template_option_path(path)`
(its base lookup found nothing AND the path is a create-template option). Generalize:

- Replace `_is_create_template_option_path` with an `is_deferred_extend_path(path)`
  predicate backed by a small registry of deferred-path matchers. Two matcher shapes:
  - **exact-depth** (create_templates options: `('create_templates', '<name>')`, depth 2);
  - **prefix** (settings_overrides: any path under `('agent_types', '<name>', 'settings_overrides')`).
- When `current is None` and the path is a deferred path, preserve the `__extend` key
  verbatim (as today for templates) for later resolution against a runtime base.
- The **resolution site stays per-consumer**: `create_templates` resolves at
  `apply_create_template` (create time, against command params); `settings_overrides`
  resolves at provision (against `B`). Only the config-load **preservation** is shared.
- De-dup `_apply_template_extend` (a bespoke copy of `_apply_extend`'s leaf logic) into a
  single shared `_apply_extend` used by both consumers.

## Cross-scope condensation of preserved markers (the part to get right)

`settings_overrides` is a `dict[str, Any]` field whose `merge_with` is assign-by-default:
a higher scope's whole `settings_overrides` replaces the lower scope's. That is wrong when
both scopes carry preserved `__extend` for the same key -- the higher would clobber the
lower's additions. The condensation must instead behave like the left fold. Rules
(applied per nested key, recursively, as scopes fold low->high):

- **higher bare key vs lower anything** -> the bare key **wins** (assign). It also removes a
  lower preserved `key__extend` for the same path (a bare assign replaces the base below,
  including its pending extend). Narrowing applies vs the lower value at provision-time only
  when the lower value is a concrete aggregate; vs a *preserved marker* there is nothing
  concrete to narrow yet.
- **higher `key__extend` vs lower bare key** -> resolve the extend against the lower bare
  value now (normal one-level extend); result stays **bare** (it still replaces `B`).
- **higher `key__extend` vs lower `key__extend`** -> **combine** into one preserved
  `key__extend` whose value is `_apply_extend(lower_value, higher_value)` (extend-of-extends:
  concat/union/one-level-merge). Stays preserved (still destined for `B`).
- **higher `key__extend` vs lower absent** -> preserved (destined for `B`).

Net invariant: after config-load, `settings_overrides` is one dict in which each key is
either a concrete **bare** value (assign vs `B`) or a single `key__extend` (extend vs `B`),
never both for the same key.

Implementation note: this likely means `settings_overrides` (and any deferred-path field)
needs a **combine-not-assign** merge for `__extend`-bearing keys in `merge_with` (or the
condensation handled in `resolve_extends` against the accumulated config before
`merge_with`). The exact seam (extend the `merge_with` carveout vs do it in
`resolve_extends`) is an implementation choice; the tests below pin the *behavior*, not the
seam.

## Provision-time resolution against `B`

In `_build_settings_json` (replacing `data = deep_merge_settings(data, settings_overrides)`):

1. Build `B` = base (home/defaults) + unattended flags + mngr hooks (as today).
2. `resolved = resolve_extends(B, settings_overrides)` -- preserved `key__extend` resolves
   against `B`'s value (extend); bare keys pass through.
3. Run the narrowing guard: for each bare key in `settings_overrides`,
   `would_assignment_narrow(B[key], settings_overrides[key])` -> if it drops a `B` entry,
   raise the standard narrowing error unless `allow_settings_key_assignment_narrowing`.
4. Overlay: `data = {**B, **resolved}` (top-level; depth already done inside
   `resolve_extends`/`_apply_extend`).
5. **Assert no `__extend` key remains** anywhere in `data` (deferred-consumption check).

## Deferred-consumption enforcement

"Everything deferred must be picked up later" -- enforce, not assume:

- **Config-load:** after parsing, any surviving `__extend` key in the resolved config must be
  inside a **registered** deferred subtree. A stray `foo__extend` on a non-deferred field
  still errors at load (as today). (Recursively scan the parsed structure / the open dict
  fields.)
- **Per consumer:** after a consumer resolves its deferred markers, assert its output has
  **zero** `__extend` keys -- `settings_overrides` at the end of `_build_settings_json`
  (step 5), `create_templates` after `apply_create_template`. A leftover marker means a
  registered deferred path had no consumer -> loud failure.
- **Unit/ratchet test:** enumerate the deferred-path registry and assert each entry has a
  wired consumer that consumes it (so adding a deferred path without a consumer fails CI).

## Worked examples == the up-front tests

`B.permissions = {"defaultMode": "auto"}` throughout.

1. **#1647, single scope, extend.** `P: settings_overrides.permissions__extend = {allow:[X]}`.
   -> preserved at load; at provision extends `B.permissions` -> `{defaultMode: auto, allow:[X]}`.
2. **#1647, single scope, bare -> narrows.** `P: settings_overrides.permissions = {allow:[X]}`.
   -> bare; at provision assign over `B.permissions` drops `defaultMode` -> **narrowing error**
   (unless the escape hatch, which yields `{allow:[X]}`).
3. **Two scopes, extend+extend combine.** `U: permissions__extend={allow:[X]}`,
   `P: permissions__extend={deny:[Y]}` -> condense to one `permissions__extend={allow:[X],deny:[Y]}`
   -> provision -> `{defaultMode:auto, allow:[X], deny:[Y]}`.
4. **Two scopes, lower bare + higher extend.** `U: permissions={allow:[X]}` (bare),
   `P: permissions__extend={deny:[Y]}` -> resolve extend vs U now -> bare
   `permissions={allow:[X],deny:[Y]}` -> provision assign over `B` -> **narrowing** (drops
   defaultMode) -> escape-hatch result `{allow:[X],deny:[Y]}`.
5. **Two scopes, lower extend + higher bare wipes it.** `U: permissions__extend={allow:[X]}`,
   `P: permissions={deny:[Y]}` (bare) -> higher bare wipes lower preserved extend -> bare
   `permissions={deny:[Y]}` -> provision assign over `B` -> narrowing (drops defaultMode).
6. **Non-overlapping keys, no narrowing.** `B` has `permissions`; `settings_overrides` sets a
   new `model="opus"` (scalar) -> assign, no narrowing (scalar, no base aggregate dropped).
7. **Hooks coexist via list concat.** `settings_overrides.hooks.SessionStart__extend=[{group}]`
   -> extends `B.hooks.SessionStart` (mngr's readiness group) -> both groups present.
8. **Deferred-consumption failure (negative test).** Construct a preserved `__extend` on a
   non-deferred field -> errors at config-load; and a deferred marker left unconsumed ->
   the step-5 assertion fires.

Associativity check (must hold for phases to equal the single fold): examples 3/4/5 each
verify two-phase (condense then merge-`B`) == `merge(merge(merge(B,U),P))`.

## Changes (sketch)

- `key_resolver.py`: deferred-path registry + `is_deferred_extend_path`; preserve on
  `current is None and is_deferred_extend_path(path)`; de-dup `_apply_template_extend` into
  `_apply_extend`; cross-scope marker-combination (extend-of-extends; bare wipes preserved).
- `data_types.py` / `merge_with`: combine (not assign) `__extend`-bearing keys for
  deferred-path dict fields, OR move that into the resolve step -- whichever is cleaner.
- `loader.py` / parse: deferred-consumption check (no unconsumed/registered-only markers).
- `mngr_claude/plugin.py` `_build_settings_json`: replace `deep_merge_settings` with
  `resolve_extends(B, settings_overrides)` + narrowing + top-level overlay + the
  zero-marker assertion. Remove `deep_merge_settings` (+ its tests) once unused.
- Field help: `settings_overrides` description -> bare assigns (narrowing-guarded),
  `__extend` merges, against the home/base layer.

## Open implementation choices (not blockers)

- Seam for cross-scope combination: `merge_with` carveout vs `resolve_extends` accumulation.
  Pick by which keeps the marker-combination logic in one place.
- Whether the narrowing guard at provision reuses `_collect_layer_narrowing`'s plumbing or
  calls `would_assignment_narrow` directly per key (likely the latter -- there is one base
  `B`, not a layer stack).
