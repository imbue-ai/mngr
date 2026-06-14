# Routing the whole config merge through overlay (exploration / uncertainty map)

Status: **exploration.** This maps the design and the *unknowns* of replacing mngr's
pydantic-model field-by-field config merge with a serialize → pre-process → overlay-merge →
re-parse pipeline -- the "route everything we can through overlay" idea (extraction-plan step 3
in [layered-config-merge-lib-readme.md](./layered-config-merge-lib-readme.md)). It is written to
decide *whether and how* to build, not as a committed plan. It does **not** touch
`allow_settings_key_assignment_narrowing` (that flag stays; its removal is a separate deferred
decision). Builds on [overlay-typed-nodes.md](./overlay-typed-nodes.md).

## Goal and why it might be worth it

Today only three things go through overlay: `resolve_extends` (raw-dict `__extend` against the
model base), the `settings_overrides` `SettingsPatchField` combine, and `_build_settings_json`.
*Everything else* -- cross-scope `MngrConfig.merge_with` and the per-sub-model `merge_with`s,
plus `_apply_custom_overrides_to_parent_config` (`parent_type` inheritance) -- is pydantic
field-by-field assignment that `model_dump`s only to read values.

Unifying onto overlay would: collapse the duplicated per-field rule (`merge_with` vs
`_apply_custom_overrides`) into one path; make the operator algebra uniform everywhere; let the
cross-scope `settings_overrides` narrowing surface naturally (no discarded `[1]`); and make
`__extend`/`__assign`/`Static*` work in *every* field, not just the few wired today.

The cost is a rewrite of the core config-load merge path with a real correctness risk. The rest
of this doc is the honest accounting of that.

## The target pipeline

Per merge of two layers (lower, higher), both already parsed models of the same (or
subclass-compatible) type:

1. **Serialize sparse:** `higher.model_dump(exclude_unset=True)` and the accumulating
   `lower` as a full dict. `exclude_unset` is the crux -- it yields *only the fields the layer
   actually set*, which is precisely the `model_fields_set` semantics the model-level merge
   relies on (an unset field must not clobber).
2. **Pre-process** each dict into the operator language (passes below).
3. **Merge** with the overlay node algebra (`lift` → `combine`/`merge_narrowing_allowed` →
   `finalize`), accumulating narrowings.
4. **Re-parse** the merged dict back into the target model class
   (`config_class.model_validate(merged)`), which re-coerces declared types and re-runs
   validators (restoring `Static*`/`StringDerivedTuple` markers, enums, tuples).

The **key feasibility insight:** overlay's combine already *is* the "set fields win, absent keys
carry through" rule -- so `exclude_unset` sparse dicts + overlay combine reproduces
assign-by-default-of-explicitly-set-fields for free. That is why this is plausible at all.

## Pre-processing passes (dict → operator language)

- **Container-additive fields** (`agent_types`, `providers`, `plugins`, `commands`,
  `create_templates`): today merged per-key via `_merge_container_dict` (recursing each entry's
  `merge_with`). Express by recursively marking those subtrees `__extend` (`mark_subtree_extend`)
  so overlay's recursive `__extend` deep-merges per key. **Equivalence to verify** (see risks).
- **`SettingsPatchField`** (`settings_overrides`): mark `__extend` so it accumulates (today's
  `combine_patches` branch).
- **Schema-`Static`** (`ScalarStrTuple` fields, e.g. `allowed_ssh_cidrs`): re-mark `Static` from
  the field annotation (the marker doesn't survive `model_dump`).
- **`StringDerivedTuple`** (string-written `cli_args`): handled by the JSON serializer enabler
  below, so the string survives as a string and is intrinsically scalar (non-narrowing).

## Enablers

- **`StringDerivedTuple` JSON serializer (item "(b)").** Give it a `when_used="json"` serializer
  that joins tokens back to the surface string (`" ".join`, which re-splits to identical tokens).
  Then `model_dump(mode="json")` of a string-written `cli_args` is a **string scalar** -- so it
  round-trips as a string, re-parses back to `StringDerivedTuple`, and is narrowing-exempt by
  *shape* (overlay never narrows a scalar) with no marker needed at merge time. Python-mode dump
  stays a tuple (so the *current* `model_copy_update` merge is untouched -- mode-split is
  idiomatic, like `datetime`). **Blast radius to check:** `completion_writer.py:396` does
  `model_dump(mode="json")` and flattens keys; confirm `cli_args`-as-string doesn't shift
  completion keys.
- **Config-shaped serializer that flattens transparent wrappers.** `CommandDefaults.defaults` /
  `CreateTemplate.options` stash arbitrary keys one level down, so a naive `model_dump` yields
  `commands.<c>.defaults.<k>` while the config/override path is `commands.<c>.<k>`. The serializer
  must flatten these so override paths line up with base paths -- the same compensation
  `_walk_to_field` does today.

## The central risk: `model_fields_set` / None fidelity (do tests-first)

The whole thing rests on `model_dump(exclude_unset=True)` faithfully meaning "the fields this
layer set." Two ways that can be wrong:

- The loader's own note: *"parse_config sets every kwarg, often to None, so `model_fields_set`
  over-reports which fields the layer touched."* If a layer is constructed such that
  `model_fields_set` includes fields the user didn't write (set to `None`), `exclude_unset` will
  leak them into the sparse dict and clobber lower layers. The model-level merge sidesteps this
  with explicit `if override.<field> is not None` guards in several `merge_with`s.
- Round-trip of `None` vs absent: a field explicitly set to `None` vs an unset field must stay
  distinguishable through dump → merge → reparse.

**Mitigation:** write the fidelity tests first -- for representative models, assert
`model_dump(exclude_unset=True)` equals the set of fields the model-level merge would treat as
"written," across set-to-None, set-to-default, and unset cases. Only build the pipeline once
those pass. This is the make-or-break item.

### Spike result (resolved favorably)

A read-only spike settled this. Empirically:

- **Top-level `MngrConfig` over-reports completely.** Setting only `prefix` yields
  `model_fields_set` of **all 24 fields** and an `exclude_unset` dump of all 24 (the rest `None`).
  Cause: `parse_config` does `raw.pop(field, None)` for every field and passes them all to
  `model_construct`.
- **Sub-models are already faithful.** An `agent_type` block with two fields set dumps exactly
  `{parent_type, cli_args}`.

But this is **not a blocker** -- it is a removable construction choice, for two reasons:

1. **The padding exists only to feed the current None-based merge** (`_assign_scalar`). Construct
   the top-level config *sparse* (only present keys -- what the sub-parsers already do) and the
   over-reporting vanishes. Defaults are **not** lost: they are applied by the **final**
   `MngrConfig.model_validate(config_dict)` at the end of load, not by the padding.
2. **The None-vs-unset ambiguity dissolves: TOML has no null.** A user can never write `None`, so
   `None` only ever means the loader's padding sentinel. With sparse construction "absent = unset"
   is unambiguous; there is no legitimate user-set-`None` to preserve. (The padding also produces
   invalid intermediate states -- e.g. a `Path` field holding `None`, visible as a serializer
   warning -- which sparse construction removes.)

**The one coupling:** sparse construction and overlay-merge (absent = unset) must flip **together**
-- the padding and the None-based `merge_with` are a matched pair. So this is a localized, coupled
change, not a semantic dead-end. The riskiest axis is therefore **green**; the remaining work is
verification (the property-test harness below) and surface area, not feasibility.

## Other uncertainties

- **Subclass / `parent_type` reconstruction.** `_apply_custom_overrides_to_parent_config` builds
  the result as the *parent's concrete class* (`ClaudeAgentConfig`, with subclass-only fields
  like `auto_dismiss_dialogs`). The pipeline must re-parse the merged dict into the *right* class
  (`config_class.model_validate(...)`) -- mngr knows it from `resolve_agent_type`, and subclass
  fields survive dump/merge/reparse, but this is a coupling the generic algebra can't carry; the
  consumer must thread the target class.
- **Container-additive equivalence.** Does recursive `__extend` marking + `exclude_unset` exactly
  reproduce `_merge_container_dict` (which recurses each entry's *own* `merge_with`, including its
  set-fields and nested-container semantics)? Plausible but subtle; needs property tests
  (old-merge == new-merge) over nested `agent_types`/`commands` configs.
- **Surface area.** "Everything" is *every* `merge_with` in the model tree (`MngrConfig`,
  `AgentTypeConfig`, `PluginConfig`, `CommandDefaults`, `RetryConfig`, `LoggingConfig`,
  `ProviderInstanceConfig`, ...). Each has its own nuances to fold into the pre-processing. This
  is the bulk of the work, not the pipeline itself.
- **Mode interactions.** The pipeline serializes `mode="json"` (to get the `StringDerivedTuple`
  string and JSON-clean types); the *current* merge uses `mode="python"`. While both coexist,
  every type with mode-divergent serialization (datetimes, enums, `Static*`) must round-trip
  through `mode="json"` + `model_validate` without drift.
- **Performance.** A full `model_validate` per merged result (vs `model_copy_update`) on every
  scope/inheritance combine. Config load isn't hot, but it is not free.
- **Narrowing routing.** All field narrowings now come back from overlay; route them into the
  loader's existing flag-gated `_collect_layer_narrowing` aggregation (flag unchanged). This is
  where the deferred cross-scope `settings_overrides` narrowing finally surfaces for free.

## Behavior-preservation strategy

This must be a pure refactor of *results*. The strongest guard is a property test:
old-`merge_with` result == new-pipeline result, over generated configs spanning every field
kind (scalars, container dicts, `SettingsPatchField`, `Static*`/`StringDerivedTuple`, subclass
fields, `parent_type` chains). Build that harness before swapping the production path; keep the
old path until it is green across the corpus.

## Prototype result (AgentTypeConfig slice -- proven)

A read-only, additive prototype (`config/overlay_merge_prototype.py` + property test, production
untouched) validated the pipeline against `AgentTypeConfig`/`ClaudeAgentConfig.merge_with`:
**30/30 `old == new` cases pass**, existing config tests green.

Findings:

- **The core insight holds with zero ceremony.** `override.model_dump(exclude_unset=True)` +
  `base.model_dump()` + bare-`Default` `combine` reproduces assign-by-set-field directly. No
  per-scalar handling.
- **Only `settings_overrides` needed pre-processing:** rename the `SettingsPatchField` key to
  `<field>__extend` on **both** sides so the algebra does `Extend`-over-`Extend` (accumulate),
  then strip the synthetic suffix after merge. The marked field set is read generically from
  `model_fields[...].metadata` -- no field name hard-coded.
- **The one real discovery: use `lower`, not `finalize`, on the combined patch.** `merge_with`
  stores the `combine_patches` output **verbatim** (inner `permissions__extend` / `allow__extend`
  markers stay unresolved until `_build_settings_json`). `finalize` over-resolved them and
  diverged; `lower` preserves them and matches.
- **`cli_args` / `StringDerivedTuple` is a *non-issue* for the value merge.** The marker only
  affects narrowing *warnings*, which `merge_with` does not perform; pydantic `==` compares
  values (and `StringDerivedTuple == tuple`), not markers. So **the JSON-serializer enabler
  "(b)" is NOT on the critical path for the merge** -- it matters only if/when the
  `MngrConfig`-level narrowing is routed through overlay. This downgrades (b) from "step 1."
- **Subclass works:** reparse via `type(base).model_validate(...)`; subclass-only fields
  round-trip. pydantic `==` ignores `model_fields_set`, so reparse re-marking all fields as set
  is harmless.
- **Mode: python.** Matches what `merge_with` dumps; no json-mode coercion surface needed here.

So the approach is proven on the representative slice. The remaining risk is **`MngrConfig`**, not
`AgentTypeConfig`.

## Honest assessment / recommended phasing

The pipeline is *feasible* (the `exclude_unset` insight is the load-bearing reason), but the
work is dominated by reproducing every model's `merge_with` nuance via pre-processing, and the
make-or-break risk is `model_fields_set` fidelity. Suggested order, each independently valuable
and shippable:

1. **`StringDerivedTuple` JSON serializer + round-trip tests + `completion_writer` check.** Small,
   self-contained, useful on its own (`mngr config list` shows `cli_args` as written), and the
   first enabler. *Only do it if we intend to pursue the integration* -- otherwise it changes
   serialization for no current consumer.
2. **`model_fields_set` fidelity test harness.** Decides whether the rest is even safe. If it
   surfaces irreducible None-ambiguity, stop here -- that is the real blocker.
3. **Old-vs-new property-test harness** over the model tree.
4. **Pre-processing passes + the config-shaped serializer**, then swap one model family at a time
   (e.g. `AgentTypeConfig` first, since `settings_overrides` already half-lives in overlay),
   keeping the old path until green.
5. The `merge_with` / `_apply_custom_overrides` duplication dissolves as families migrate.

If step 2 is shaky or the appetite is limited, the **incremental alternative** -- migrate just
the existing `combine_patches`/`SettingsPatchField` combines to the node algebra and stop -- gets
most of the consistency benefit for a fraction of the risk, and leaves the full round-trip for
later.
