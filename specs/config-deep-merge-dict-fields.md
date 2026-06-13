# Deep-merge config dict fields across layers

Status: **spec only -- not yet implemented.** A libs/mngr config-system feature: let a
specific `dict[str, Any]` config field **deep-merge** (recursively, preserving nested
sibling keys) across config layers (user < project < local settings.toml), instead of
today's assign-by-default (full replace) or shallow `__extend`. Motivated by
`ClaudeAgentConfig.settings_overrides` (see
[claude-settings-overrides](./claude-settings-overrides.md)) and by the problem that closed
PR #1647 (a `permissions.allow`-only override wiped the sibling `permissions.defaultMode`).

Audience: developers working in `libs/mngr/imbue/mngr/config/`.

## Background

- **Cross-layer merge is per-class, assign-by-default.** Each config model has its own
  `merge_with` (`data_types.py`); there is no shared merge helper. `AgentTypeConfig.merge_with`
  applies `override.model_fields_set` over `model_copy_update`, so every aggregate field --
  including a `dict[str, Any]` -- in a higher-precedence layer **replaces** the base value
  entirely. This is the deliberate assign-by-default model from
  [env-settings-overrides](./env-settings-overrides/concise.md).
- **`__extend` is shallow for dicts.** `_apply_extend` (`key_resolver.py`) does
  `{**current, **extend}` -- a one-level key merge, no recursion. So `field__extend` across
  layers preserves *top-level* keys but a re-specified nested dict (e.g. `permissions`)
  still replaces its inner siblings. This shallowness is what blocked adopting the mngr
  merge scheme in #1647.
- **Subclass fields flow through generically.** `settings_overrides` is defined on
  `ClaudeAgentConfig` (a plugin subclass), but `AgentTypeConfig.merge_with` reads
  `override.model_fields_set` / `model_dump()`, which include subclass fields. So a base-class
  merge rule keyed on a **field annotation** (not a field name) honors a plugin's field with
  no core awareness of the plugin.
- **The narrowing guard runs before merge.** Per layer, `_collect_layer_narrowing` calls
  `detect_settings_narrowing(base, parsed_layer)` *before* `merge_with`, comparing field
  values; a higher layer assigning a smaller aggregate over a non-empty base is a **hard
  error**. Only `ScalarTuple` is exempt today (`would_assignment_narrow`). A deep-merge field
  is not auto-exempt -- the guard would still see the override dict as "smaller" and fire.
- **Native Claude deep-merges its own layers (verified).** A sibling `env` key in a project
  `.claude/settings.json` survives a different sibling set via `--settings`. So the
  on-disk-layer side of #1647 is handled by Claude itself; this spec is only about mngr's
  **config-scope** merge of `settings_overrides` (and any future opted-in field).

## Expected behavior

### Opt-in deep merge via a field annotation

- A field opts in with a marker, e.g. `Annotated[dict[str, Any], DeepMergeDict]`, defined in
  `libs/mngr` core and imported by the declaring model (so a plugin can annotate its own
  field). `merge_with` reads the marker off `model_fields[name]` and applies deep merge for
  that field; all other fields keep assign-by-default.
- The marker is **plugin-agnostic**: core `merge_with` checks "is this field marked
  deep-merge", never the field name. `ClaudeAgentConfig.settings_overrides` carries the
  marker; nothing in core references `settings_overrides`.

### Deep-merge semantics

`deep_merge_dicts(base, override)`:

- Both values dict -> recurse key by key. Keys only in `base` are **preserved** (the #1647
  fix). Keys in both: recurse if both dicts, else the override leaf wins.
- A leaf (scalar, list, or non-dict) -> **override replaces** it. Lists **assign** (replace),
  not concatenate -- deep merge preserves *dict structure*, not list accumulation. (List
  concat stays the `__extend` story; see Open Question 1.)
- Pure function, no input mutation (mirror `_apply_extend`'s non-mutation contract).

So across user < project < local, `settings_overrides` condenses: every layer's nested
sibling keys survive unless a higher layer re-specifies that exact leaf.

### Narrowing guard

- A deep-merge-marked field is **exempt** from the narrowing guard: by construction it never
  drops a base *dict key* (siblings are preserved), and a re-specified leaf list/scalar is
  intentional replacement, not aggregate narrowing -- mirroring the existing `ScalarTuple`
  exemption rationale. Add the marker to `would_assignment_narrow` / `_check_narrowing`'s
  exemption check.
- This is the whole point: the user opts into deep merge precisely to make "set a nested key
  without wiping its siblings" the default, with no narrowing error.

### `__extend` interaction

- `field__extend` on a deep-merge field is **rejected** at config-load with a clear message
  ("`settings_overrides` deep-merges across layers by default; remove `__extend`"). Bare
  assignment already condenses, so `__extend` would be redundant and would otherwise apply
  the *shallow* `_apply_extend` path inside `resolve_extends` before the deep `merge_with`,
  producing inconsistent semantics. Bare `settings_overrides` passes through `resolve_extends`
  untouched (Claude keys carry no `__extend` suffix) and is deep-merged by `merge_with`.

### Worked example

```toml
# user settings.toml
[agent_types.coder.settings_overrides.permissions]
defaultMode = "auto"

# project settings.toml
[agent_types.coder.settings_overrides.permissions]
allow = ["Bash(npm *)"]
```
Resolved `coder.settings_overrides`:
`{"permissions": {"defaultMode": "auto", "allow": ["Bash(npm *)"]}}` -- both siblings
survive. Today (assign): only `{"permissions": {"allow": [...]}}` (defaultMode lost -- the
#1647 bug). Today (`settings_overrides__extend`): same loss, since the shallow merge replaces
the whole `permissions` value.

## Changes

`libs/mngr/imbue/mngr/config/data_types.py`:

- Define the `DeepMergeDict` marker (a small marker class usable in `Annotated[...]`).
- Add `deep_merge_dicts(base, override)` (pure, recursive; leaves/lists assign).
- In `AgentTypeConfig.merge_with`: for each explicitly-set field, if it carries the
  `DeepMergeDict` marker and both base and override values are dicts, `deep_merge_dicts`
  them; otherwise assign as today. (Only this `merge_with` is needed for `settings_overrides`;
  other models keep assign-by-default unless/until a field there opts in.)
- Exempt `DeepMergeDict`-marked fields in `would_assignment_narrow` / `_check_narrowing` /
  `detect_settings_narrowing`.

`libs/mngr/imbue/mngr/config/key_resolver.py`:

- Reject `__extend` on a `DeepMergeDict`-marked field with a clear `ConfigParseError`
  (requires resolve_extends to know which fields are marked -- pass the marked-field set, or
  check the base model's `model_fields`; see Open Question 2).

`libs/mngr_claude/imbue/mngr_claude/plugin.py`:

- Annotate `ClaudeAgentConfig.settings_overrides` with `DeepMergeDict` and update its
  description (deep-merges across config layers; no `__extend`).

Docs / changelog: document the new field-merge policy in the config docs (alongside
assign-vs-extend); changelog entry noting `settings_overrides` now deep-merges across scopes
(behavior change vs assign/replace).

## Open questions

1. **Leaf lists: assign or concat?** This spec assigns (replaces) leaf lists inside a
   deep-merged dict; list accumulation stays the `__extend` story. Confirm that's the desired
   default for `settings_overrides` (e.g. `permissions.allow` across scopes replaces rather
   than accumulates). If accumulation is wanted, it needs a separate per-leaf opt-in and is
   out of scope here.
2. **Where `resolve_extends` learns the marked fields.** It runs before parse, on a raw dict,
   against a `base_config` model. To reject `field__extend` on a marked field it needs the set
   of marked field paths. Options: (a) derive from `base_config.__class__.model_fields`
   recursively at call time; (b) pass a precomputed marked-path set from the loader. Prefer
   (a) if cheap. (If rejecting is too invasive, the fallback is to *honor* `__extend` as a
   deep merge for marked fields -- but rejecting is clearer.)
3. **Generalization to other models.** Only `AgentTypeConfig.merge_with` is changed here.
   If other models later want deep-merge fields, the marker + `deep_merge_dicts` are reusable,
   but each `merge_with` must add the check. Acceptable (there's no shared merge helper to
   centralize it); note it so the pattern is consistent if extended.

## Tests

`libs/mngr/imbue/mngr/config/data_types_test.py`:

- `deep_merge_dicts` unit: recurse preserves siblings; leaf/list/scalar assign; no mutation;
  disjoint keys union; nested-3-levels.
- `AgentTypeConfig.merge_with` with a deep-merge field: nested sibling survives across two
  layers (the #1647 case); a non-marked dict field still assign-replaces; subclass field
  (`settings_overrides`) honored by base `merge_with`.
- Narrowing: a deep-merge field that "shrinks" a nested dict does **not** raise; a non-marked
  field in the same config still does.

`libs/mngr/imbue/mngr/config/key_resolver_test.py`:

- `field__extend` on a marked field raises `ConfigParseError` with the guidance message.

`libs/mngr/imbue/mngr/config/loader_test.py`:

- End-to-end user/project/local layering of `settings_overrides`: nested siblings condense
  across all three scopes; verify the resolved value and that no narrowing error fires.
