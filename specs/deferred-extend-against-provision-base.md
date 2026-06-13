# Config-consistent `settings_overrides` via a recursive `__extend` fold

Status: **spec only -- not yet implemented.** Makes `settings_overrides` merge onto the
user's home Claude settings with mngr's normal config semantics (bare = assign + narrowing
guard; `__extend` = merge), by treating it as a **patch** folded onto the provision base.
Generalizes the `create_templates` deferred-`__extend` precedent. Part of the
`mngr/claude-hook-leak` PR. Supersedes this file's earlier cross-scope-combination-as-
special-case draft.

Audience: developers in `libs/mngr/imbue/mngr/config/` and `libs/mngr_claude`.

## The model: one associative `fold`, B at the bottom

The agent's effective Claude settings are a single left fold, lowest precedence first:

```
fold( fold( fold( fold(B, U), P), L), <env>, <--setting> )
```

- `B` = the **provision base**: synced `~/.claude/settings.json` (or generated defaults) +
  unattended flags + mngr's own hooks. Built at **provision**. Concrete (no markers).
- `U/P/L/...` = each config scope's `settings_overrides` (a **patch**, may contain `__extend`
  markers at any depth).
- `fold(acc, patch)` applies mngr's standard per-key rules **recursively**:
  - **bare `key`** in patch -> **assign**: replaces `acc[key]` (and any `acc[key__extend]`).
    If `acc[key]` is a non-empty aggregate that the assignment would drop entries from, the
    **narrowing guard** hard-errors (unless `allow_settings_key_assignment_narrowing`).
  - **`key__extend`** in patch -> **extend** `acc[key]`:
    - `acc[key]` concrete: extend it (list concat / set union / **recurse** for dicts,
      threading `acc[key]` as the base for the nested patch).
    - `acc[key]` absent: extend-of-nothing acts as **assign** (`acc[key]` = the value, with
      its own nested markers resolved against an empty base). (Existing `_apply_extend`
      `current is None` branch.)
    - `acc[key__extend]` present (i.e. `acc` is itself a patch, cross-scope case):
      **combine** the two markers -> `fold` their values, keep the `__extend` (still a patch).

Because `fold` is **associative** (proof below), the scopes can be condensed into one patch
at config-load and applied to `B` at provision -- same result as folding `B` first:

```
fold(B, fold(fold(U, P), L))  ==  fold(fold(fold(B, U), P), L)
```

This is the whole design: `settings_overrides` is a **patch**; config-load **combines** the
per-scope patches into one (markers preserved/combined); provision **applies** the combined
patch to `B` (markers all resolved). Same `fold` in both phases -- it branches on whether the
accumulator's value at a key is concrete or itself a marker.

### Associativity (the four cases) -- these become tests

For a key `f`, with `B[f] = V` (or absent), lower patch `X`, higher patch `Y`:

| X | Y | `combine(X,Y)` | `fold(B, combine)` | `fold(fold(B,X),Y)` |
|---|---|---|---|---|
| `f__extend=A` | `f__extend=B` | `f__extend = A⊕B` | `V⊕A⊕B` | `(V⊕A)⊕B` |
| `f=A` | `f__extend=B` | `f = A⊕B` | `A⊕B` | `A⊕B` |
| `f__extend=A` | `f=B` | `f = B` | `B` | `B` |
| `f=A` | `f=B` | `f = B` | `B` | `B` |

`⊕` is the per-type extend (list concat / set union / recursive dict fold). Rows 1/2 rely on
`⊕`'s associativity; 3/4 on "higher bare wins" (a bare key drops a lower marker for the same
key). The table applies recursively for nested dict values.

## Back-compatibility of recursive `__extend`

Today `__extend` is one-level (env-settings-overrides: "No recursion into nested
aggregates"). Making it recursive is **backward-compatible for every input that does not nest
an `__extend` inside an `__extend` value**:

- A bare nested key behaves identically: old shallow `{**current, **value}` *replaces*
  `current`'s keys at that level; new "bare = assign at that level" does the same (including
  dropping nested siblings -- to keep them you write the nested `__extend`).
- The only inputs whose meaning changes are ones with an `__extend` **inside** an `__extend`
  value (e.g. `permissions__extend = {allow__extend: [...]}`). Under the old operator those
  were never meaningful -- an unknown `__extend` key on a typed field (error) or a literal
  garbage `"allow__extend"` key forwarded to Claude on a schemaless field. They now gain the
  intended recursive meaning.

So no existing, meaningful config changes behavior. (A test pins this invariant.)

## Where each phase lives

- **`resolve_extends` (config-load):** does **not** resolve `__extend` inside a deferred-path
  subtree (`settings_overrides`); it leaves those markers intact for the provision fold.
  (Generalize `_is_create_template_option_path` into an `is_deferred_extend_path` predicate
  backed by a small registry: exact-depth for `create_templates` options, **prefix** for
  `agent_types.<name>.settings_overrides`.) Markers on non-deferred fields resolve as today.
- **`merge_with` (config-load):** for a deferred-path dict field (`settings_overrides`),
  **combine** the patches across scopes via `fold`'s combine mode instead of assign-by-default.
  This is the only field-merge behavior change, and only for deferred-path fields.
- **provision (`_build_settings_json`, mngr_claude):** build `B` (home/defaults + flags +
  mngr hooks), then `data = fold(B, agent_config.settings_overrides)` -- resolve all markers
  against `B`, applying the narrowing guard for bare keys. Replaces `deep_merge_settings`.
- **`create_templates`:** keeps deferring, now via the shared registry; de-dup
  `_apply_template_extend` into the shared recursive `fold`/`_apply_extend`.

## Narrowing guard at provision

The guard currently runs in the loader across config layers. Add a call in the provision
fold: when a bare `settings_overrides` key assigns over a non-empty `B[key]` aggregate and
drops an entry, `would_assignment_narrow(B[key], value)` -> raise the standard narrowing
error unless `allow_settings_key_assignment_narrowing`. (One base `B`, so call
`would_assignment_narrow` directly per key; no layer-stack plumbing.)

## Deferred-consumption enforcement

- **Final assertion (schema-independent):** after the provision fold, assert the built
  `settings.json` contains **no** `__extend` key anywhere. This always holds when `B` is
  concrete -- every marker resolves (extend-against-present -> merge; extend-against-absent
  -> assign), so a survivor indicates a **fold bug**, not a user typo. This is the
  "everything deferred is picked up" guarantee.
- **Registry/consumer test:** a unit test enumerates the deferred-path registry and asserts
  each entry has a wired consumer (adding a deferred path without a consumer fails CI).
- Note: because `settings_overrides` is schemaless, mngr cannot validate at config-load that
  a preserved `key__extend` names a real Claude key (a typo is forwarded). That is the
  accepted schema-free tradeoff; the final assertion still guarantees no *marker* leaks to
  Claude (a typo'd `fooo__extend` resolves to a bare `fooo` key -- garbage, but Claude's
  problem, not a marker).

## The base is explicitly normalized (dissolves the `__extend`-in-base wart)

`__extend` is an mngr config operator (`settings.toml`, `--setting`, env, `mngr config`). The
home `~/.claude/settings.json` is **Claude's own file** and the bottom of the fold, where
`__extend` has nothing below it to extend. Rather than special-case or warn about a stray
`__extend` there, **normalize `B` up front**: in the fold model the true bottom is the empty
dict, and `B_raw` (home settings + flags + mngr hooks) is just the *first patch*, so

```
B = fold({}, B_raw)
```

resolves any `__extend` in `B_raw` against nothing -- which strips the suffix (extend-against-
empty = assign). After this `B` is **concrete by construction** (no markers), so:

- the fold invariant ("`B` concrete at the bottom") holds without a runtime check;
- the zero-marker assertion on the final output is clean (only patch markers had to resolve,
  and they all do against a concrete `B`);
- a stray `permissions__extend` in someone's home `settings.json` simply degrades to a plain
  `permissions` key -- no warning, no crash, no special case.

mngr's own contributions to `B_raw` (flags, hooks) never carry markers, so in practice only
the home file could contribute one, and it normalizes away harmlessly. Declare `B` explicitly
in `_build_settings_json` and normalize it before folding the `settings_overrides` patch.

## Worked examples == the up-front tests

`B.permissions = {"defaultMode": "acceptEdits"}` unless noted.

1. **Back-compat invariant.** `permissions__extend = {allow: [X], deny: [Y]}` (no nested
   markers) -> identical result under old and new: `{defaultMode, allow:[X], deny:[Y]}`.
2. **#1647, single scope, nested extend.** `permissions__extend = {allow__extend: [X]}` ->
   `{defaultMode, allow:[X]}` (home `defaultMode` preserved).
3. **#1647, single scope, bare -> narrows.** `permissions = {allow:[X]}` -> bare assign drops
   `defaultMode` -> **narrowing error** (escape hatch yields `{allow:[X]}`).
4. **Cross-scope extend+extend accumulate.** `U: permissions__extend={allow__extend:[X]}`,
   `P: permissions__extend={allow__extend:[Y]}` -> combine -> provision ->
   `{defaultMode, allow:[X,Y]}`.
5. **Cross-scope lower-bare + higher-extend.** `U: permissions={allow:[X]}` (bare),
   `P: permissions__extend={allow__extend:[Y]}` -> combine -> bare `permissions={allow:[X,Y]}`
   -> provision assign over B -> **narrowing** (drops defaultMode).
6. **Cross-scope higher-bare wipes lower-extend.** `U: permissions__extend={allow__extend:[X]}`,
   `P: permissions={allow:[Y]}` (bare) -> combine -> bare `permissions={allow:[Y]}` ->
   provision -> **narrowing**.
7. **Associativity.** Examples 4/5/6: assert `fold(B, combine(U,P)) == fold(fold(B,U),P)`.
8. **Hooks coexist (list concat).** `settings_overrides.hooks__extend.SessionStart__extend =
   [{group}]` -> `hooks__extend` merges onto B's hooks dict (preserving mngr's other events
   like `UserPromptSubmit`), and `SessionStart__extend` concats onto B's readiness
   `SessionStart` list -> both groups present, sibling events preserved. (Note: a *bare*
   `hooks` intermediate would assign-replace B's whole hooks dict, dropping mngr's other
   events and tripping the narrowing guard -- `__extend` must be marked at each level you
   want merged, including `hooks` itself.)
9. **Non-overlap scalar.** `settings_overrides.model = "opus"` over a B without `model` ->
   assign, no narrowing.
10. **Zero-marker output.** After any of the above, the built `settings.json` has no
    `__extend` key. Negative: a deliberately-unconsumed marker (simulated fold bug) trips the
    assertion.
11. **Base normalization.** A home `settings.json` containing a literal `permissions__extend`
    -> `B = fold({}, B_raw)` strips it to a plain `permissions` key; the build succeeds and
    the output has no marker (no warning, no crash).

## Changes (sketch)

`libs/mngr/imbue/mngr/config/key_resolver.py`:

- Make `_apply_extend`'s dict branch **recursive** (the `fold`): for each key in the extend
  value, `key__extend` -> recurse/extend against `current[key]`; bare -> assign. Lists concat,
  sets union, scalars assign (unchanged). This is the single shared extend primitive.
- `is_deferred_extend_path(path)` + registry (exact-depth `create_templates`; prefix
  `settings_overrides`); `resolve_extends` preserves markers inside deferred subtrees.
- De-dup `_apply_template_extend` into the shared primitive.

`libs/mngr/imbue/mngr/config/data_types.py`:

- `merge_with` for deferred-path dict fields: **combine** patches via the fold (preserve/combine
  markers, higher-bare-wins) rather than assign-by-default.

`libs/mngr/imbue/mngr/config/loader.py`:

- (If needed) thread the deferred-path registry; no new narrowing call here.

`libs/mngr_claude/imbue/mngr_claude/plugin.py` (`_build_settings_json`):

- Replace `deep_merge_settings(data, settings_overrides)` with `fold(B, settings_overrides)`
  (resolve markers against `B`) + per-key narrowing + the zero-marker assertion. Remove
  `deep_merge_settings` (and its tests) once unused.

Field help: `settings_overrides` -> "a patch merged onto your home Claude settings: a bare
key replaces (and warns if it drops a sibling); `key__extend` merges; nest `__extend` to
merge deeper."

## Open implementation choices (not blockers)

- Whether the cross-scope combine lives in `merge_with` or in a `resolve_extends`
  accumulation step -- pick whichever keeps the marker logic in one place. (Tests pin
  behavior, not seam.)
- The `fold` primitive likely belongs in `key_resolver.py` (shared by config core and, via
  import, by `mngr_claude`'s provision step). Confirm no layering issue importing it into
  `mngr_claude`.
