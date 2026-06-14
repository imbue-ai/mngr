# mngr config merge

How mngr builds one effective `MngrConfig` from many layers. mngr expresses its
merge semantics with the generic, framework-agnostic algebra in the
[`overlay`](../../../../overlay/README.md) library; **this document owns mngr's
*application* of that algebra**, not the algebra itself. Read the overlay README
first for the operator language (`__extend` / `__assign` / `Static*`), the typed
nodes, narrowing, and associativity. Everything below is mngr-specific wiring.

## The config surface

A user expresses overrides through several surfaces, all of which compile down to
the same operator-keyed dict before merging:

- **TOML** settings files (`settings.toml`, `settings.local.toml`).
- **`MNGR__*` environment variables** -- each `__`-separated segment after `MNGR__`
  is one dotted-key segment, lowercased; a trailing `__EXTEND` segment becomes the
  `__extend` operator. See [`docs/concepts/environment_variables.md`](../../../docs/concepts/environment_variables.md).
- **`--setting KEY=VALUE`** CLI overrides and **`mngr config set|extend`**.

`resolve_extends` (`key_resolver.py`) is the single place that recognises the
operator suffixes across every surface; it runs before `parse_config` validates the
dict into a model. Field-name normalisation (hyphens to underscores, `__` reserved
for operator suffixes) lives in `loader.py` (`_normalize_field_keys`).

## Precedence

Layers merge lowest to highest (`load_config`):

1. Built-in `MngrConfig` defaults
2. User config (`~/.<root>/profiles/<id>/settings.toml`)
3. Project config (`.<root>/settings.toml`)
4. Local config (`.<root>/settings.local.toml`)
5. `MNGR__*` env vars (plus preserved aliases `MNGR_PREFIX` / `MNGR_HOST_DIR` / `MNGR_HEADLESS`)
6. `--setting KEY=VALUE` CLI overrides
7. CLI arguments

The narrowing guard (below) runs over layers 2-5 only. Layer 6 (`--setting`) is
merged afterward in `setup_command_context` because its `__extend` keys must resolve
against the already-loaded config -- so `allow_settings_key_assignment_narrowing` can
be opted into via a settings file or `MNGR__*`, but not via `--setting`.

## The model-merge pipeline (`merge_models_via_overlay`)

`overlay_merge.py` is the bridge between mngr's pydantic models and the overlay
algebra. Every config-model merge -- `AgentTypeConfig.merge_with`,
`MngrConfig.merge_with`, and `parent_type` inheritance -- goes through
`merge_models_via_overlay`, which reproduces the old field-by-field pydantic merge
via **serialize -> pre-process -> overlay merge -> reparse**:

1. **Serialize.** `base.model_dump()` (full) and `override.model_dump(exclude_unset=True)`
   (sparse -- only the fields this layer actually set, the `model_fields_set` semantics
   the merge relies on). `serialize_as_any` keeps subclass container entries (e.g.
   `ClaudeAgentConfig`) serialising through their concrete type.
2. **Pre-process** the sparse override into the operator language:
   - A `SettingsPatchField` (see below) becomes `<field>__extend`, so the algebra
     *accumulates* it instead of assigning.
   - Each *container-additive* field (`agent_types`, `providers`, `plugins`,
     `commands`, `create_templates`) becomes a **two-level `__extend`** -- the
     container itself, and each entry key -- so overlay deep-merges per key and a
     shared-key entry combines field-by-field (which is the entry's own merge).
   - When `drop_none_values` is set, keys that are `None` on both sides are dropped:
     `parse_config` pads every unset scalar to `None` and TOML has no null, so a
     `None` is always *unset* (reproducing the old `_assign_scalar` / None-base guards).
   - Every other key stays bare, which the algebra lifts to a `Default` (assign).
3. **Merge.** `lift` both dicts and `merge_narrowing_allowed` override over base.
4. **Lower + reparse.** `lower` (not `finalize`) preserves the accumulated inner
   `__extend` markers in the settings patch; strip the synthetic suffixes, reparse
   each container entry into its concrete (sub)class, then reparse the whole dict into
   `type(base)` so a subclass stays its concrete class with its subclass-only fields.

## Settings patches (`SettingsPatchField`)

A dict field annotated `Annotated[dict[str, Any], SettingsPatchField()]`
**accumulates** across layers rather than assigning: non-overlapping keys from every
layer survive and same-key `__extend`s combine. The merge reads the marker off the
pydantic field metadata, so core never hard-codes the field name. Because such a
field can only grow (a higher layer adding keys is a superset), it is also **exempt
from the assign-narrowing detector**. The only field carrying it today is
`ClaudeAgentConfig.settings_overrides`.

## `parent_type` inheritance

`_apply_custom_overrides_to_parent_config` (`agent_config_registry.py`) folds a
custom agent type's `[agent_types.X]` block onto its parent type's config through the
*same* pipeline, with two wrinkles: the routing-metadata fields (`parent_type` /
`plugin`) are dropped from the child's dump, and the result reparses into
`type(parent)` -- the class-switching crux, so a base-class child folded onto a
`ClaudeAgentConfig` parent yields a `ClaudeAgentConfig` with the parent's
subclass-only fields.

## Deferred resolution

Some `__extend` / `__assign` markers cannot resolve at config-load because their base
is only built at runtime. `resolve_extends` preserves them verbatim for a small
registry of paths, each with a wired consumer (`key_resolver.py`):

- `create_templates.<name>` -> `apply_create_template` (at `mngr create` time).
- `agent_types.<name>.settings_overrides` -> `_build_settings_json`
  (`mngr_claude/plugin.py`), folded against the provisioned home `settings.json`.

`__assign` preservation is scoped to `settings_overrides` only, because its consumer
re-lifts the stored patch and honours the no-warn `Assign`; `create_templates` reads
only `__extend`. See the overlay README's "deferred resolution against a runtime
base" for why this is sound (associativity).

## Narrowing at config-load

Two disjoint paths surface a narrowing (a higher layer's bare assign silently
dropping a non-empty aggregate from a lower layer):

- **Assign-by-default fields** -- `detect_settings_narrowing` (`data_types.py`) walks
  the merged models. A `ScalarTuple` / `StringDerivedTuple` (a string-shaped TOML
  value coerced to a tuple) is a scalar, not an aggregate, so replacing it is exempt;
  container dict fields are matched by **fully-qualified path** (not bare name) so a
  same-named field nested in a sub-model is checked as a leaf.
- **`SettingsPatchField` fields** -- exempt from the walker above (they accumulate),
  so their cross-scope narrowings come from the overlay merge instead:
  `merge_models_via_overlay_with_narrowings` returns the settings-patch narrowing
  paths, which the loader routes into the same aggregation.

Both feed the flag-gated error: collected violations raise unless
`allow_settings_key_assignment_narrowing` is set (a transitional escape hatch;
per-key, prefer `key__extend` to accumulate or `key__assign` to replace without warning).
