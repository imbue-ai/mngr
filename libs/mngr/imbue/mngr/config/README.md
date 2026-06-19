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
2. **Pre-process** the sparse override into the operator language by one uniform walk of
   the live model and its dumped dict (the live value at each key selects the rule):
   - A *sub-model* field (one whose **live value is a `BaseModel`**, e.g.
     `logging` / `retry` or a provider's `security_group`) becomes `<field>__extend`
     and is recursed into, so it merges **field-by-field** -- the base's unset
     sub-fields carry through rather than reverting to defaults. Detection is purely
     runtime (`isinstance(value, BaseModel)`): a `None` simply is not a model, and a
     discriminated-union value's concrete class is its own `type()`. Conceptually
     distinct from a settings patch (which accumulates keys) even though both emit
     `__extend`. A nested *aggregate* inside a sub-model still narrows when it drops
     entries.
   - Each *registry* field marked `RegistryField` (`agent_types`, `providers`,
     `plugins`, `commands`, `create_templates`) becomes a **two-level `__extend`** --
     the dict itself, and each entry key -- so overlay deep-merges per key and a
     shared-key entry combines field-by-field (which is the entry's own merge). A
     registry entry's own sub-model fields are marked `__extend` too. The marked names
     come from `get_registry_field_names` (no hard-coded set).
   - A `SettingsPatchField` (see below) becomes `<field>__extend`, so the algebra
     *accumulates* it instead of assigning.
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
from the assign-narrowing detector**. The fields carrying it today are
`ClaudeAgentConfig.settings_overrides` and `AntigravityAgentConfig.settings_overrides`.

### The `__mngr_merge` surface for `settings_overrides`

`settings_overrides` is folded into a file an external AI CLI reads (Claude Code's /
antigravity's `settings.json`). That CLI does not understand the `__extend` / `__assign`
leaf suffixes and would treat `permissions__extend` as a junk literal key, so the
suffixes are **rejected** on this path. Instead, merge intent is declared in a single
top-level `__mngr_merge` map (dotted key path -> `"extend"` | `"assign"`), which the
external CLI silently ignores:

```toml
[agent_types.claude.settings_overrides.permissions]
allow = ["Bash(npm *)"]
[agent_types.claude.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"   # merge onto the base; "assign" replaces without the guard
```

All of this lives in one self-contained module, **`external_settings.py`** ("logic for a
settings file owned by an external tool"); the config-tree wiring just calls it.
`desugar_settings_overrides` rewrites the map at config-load into the internal suffix form
(the targeted leaf takes the operator's suffix; every ancestor takes `__extend` so the
recursive merge reaches it), so the rest of the algebra is unchanged. A bare key (absent
from the map) stays a narrowing-checked assign. The one-operator-per-path model
intentionally drops the within-layer reset-then-add idiom (inexpressible in the clean JSON
an external CLI reads). `mngr config extend|assign <path> <value>` writes these directives
for you (bare value + a `__mngr_merge` entry) on a `settings_overrides` path.

The provision-time fold (`apply_settings_patch`, shared by both plugins) strips a stray
`__mngr_merge` from the base (a no-op on the floor) and, on a narrowing, reports the exact
`__mngr_merge` patch to add. The remediation reports the full nested patch even where
`narrowing_paths` stops at the dict level: a dict that drops a sibling key is suggested as
`extend` (so the sibling survives) and a replaced list/value as `assign`. Because
`__mngr_merge` keys are dotted paths, a settings key that contains a *literal* dot (e.g. an
MCP server name like `my.server`) cannot be targeted: such a directive errors as dangling,
and the auto-remediation skips it rather than mis-advising.

## Registries (`RegistryField`)

A top-level dict-of-models field annotated `Annotated[dict[K, V], RegistryField()]`
merges **per key** instead of assigning by default: a key set in one scope carries
through, and a shared key has its entries merged field-by-field (the entry's own merge).
The five `MngrConfig` registries -- `agent_types`, `providers`, `plugins`, `commands`,
`create_templates` -- carry the marker; the merge reads it off the field metadata
(`get_registry_field_names`), so there is no hard-coded container set. Parallel to
`SettingsPatchField` but a distinct concept (registries merge entries per key; a settings
patch accumulates keys). A same-named dict *nested inside* a sub-model (e.g. a plugin
config's own `commands` dict) carries no marker and is an ordinary assign-by-default
aggregate, narrowing-checked as a leaf.

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
- `agent_types.<name>.settings_overrides` -> `external_settings.apply_settings_patch`
  (via `mngr_claude` / `mngr_antigravity`), folded against the provisioned base
  `settings.json`. Markers here arrive desugared from the `__mngr_merge` surface (above).

`__assign` preservation is scoped to `settings_overrides` only, because its consumer
re-lifts the stored patch and honours the no-warn `Assign`; `create_templates` reads
only `__extend`. See the overlay README's "deferred resolution against a runtime
base" for why this is sound (associativity).

## Narrowing at config-load

A narrowing is a higher layer's bare assign silently dropping a non-empty aggregate
that a lower layer set. **All** narrowing detection flows through the one overlay
merge: `merge_models_via_overlay` returns every narrowing path it
finds, and the loader (`_collect_narrowing`) attributes each to the layer that set
the dropped value. There is no separate model-walker.

The overlay's `narrowing_paths` does the detection (in `imbue.overlay.narrowing`):

- An override that is a superset, a no-op, or a `Static*` atomic aggregate does **not**
  narrow. A `ScalarTuple` (a string-shaped TOML value coerced to a tuple, e.g. a
  string-written `cli_args`) is a `Static*`, so replacing it is a value-set, not
  narrowing. Because
  `model_dump` strips the `Static*` subclass back to a plain aggregate, the pipeline
  re-marks those values on the override before merging (see `_collect_static_marker_paths`
  / `_remark_static_leaves`) -- which relies on the markers' proven-pure round-trip.
- A `SettingsPatchField` accumulates rather than assigns, so a higher layer is always a
  superset of it *except* for an in-patch bare drop, which `narrowing_paths` reports like
  any other -- the cross-scope `settings_overrides` narrowing.
- Reported paths point at the narrowed leaf: a dropped dict key or a narrowed
  list/set is reported at its field; a nested dict whose value narrows reports the deep
  path (e.g. `commands.create.defaults.env`).

Collected violations feed the flag-gated error: they raise unless
`allow_settings_key_assignment_narrowing` is set (a transitional escape hatch;
per-key, prefer `key__extend` to accumulate or `key__assign` to replace without warning --
or, inside `settings_overrides`, the `__mngr_merge` map above).
