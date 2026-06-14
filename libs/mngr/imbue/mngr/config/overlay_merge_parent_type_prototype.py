"""PROTOTYPE -- not wired into any production code path.

Completes the proof-of-approach for the spec in
``specs/whole-config-overlay-integration.md`` by reproducing the **last** merge
axis: the ``parent_type`` inheritance path,
``agent_config_registry._apply_custom_overrides_to_parent_config``. This is the
class-switching variant the two prior prototypes (``overlay_merge_prototype.py``
for ``AgentTypeConfig.merge_with`` and ``overlay_merge_mngr_prototype.py`` for
``MngrConfig.merge_with``) deliberately left out.

``_apply_custom_overrides_to_parent_config(parent_config, custom_config)`` applies
the *child*'s explicitly-set fields onto the *parent*'s config, but constructs the
result as the **parent's** concrete class (``type(parent_config)``). So for a
``custom_config`` whose ``parent_type = "claude"``, the output is a
``ClaudeAgentConfig`` even though ``custom_config`` carries the child's field
values. ``resolve_agent_type`` calls it twice: first
``_apply_custom_overrides_to_parent_config(config_class(), parent_user_config)`` to
fold the parent type's own ``[agent_types.claude]`` user block onto bare defaults,
then ``_apply_custom_overrides_to_parent_config(parent_base_config, custom_config)``
to fold the child onto that.

It differs from ``AgentTypeConfig.merge_with`` in exactly three ways, which is why
the pipeline is the ``overlay_merge_prototype.py`` pipeline with three deltas:

1. **Routing metadata is skipped.** The iteration is over
   ``custom_config.model_fields_set`` *minus* ``_METADATA_FIELDS`` =
   ``{parent_type, plugin}``. Those two fields are inheritance routing, not runtime
   config, so the child's ``parent_type`` / ``plugin`` never land on the parent's
   config. The pipeline reproduces this by **dropping** those two keys from the
   override (custom) sparse dump before merging.

2. **The output class is the parent's, not the override's.** ``merge_with`` returns
   ``type(self)``; here ``self`` *is* the parent (``model_copy_update`` on
   ``parent_config``), so the class follows the parent. The pipeline already
   re-parses into ``type(parent_config)``, so this falls out for free -- but it is
   the class-switching crux: a base-class ``custom_config`` folded onto a
   ``ClaudeAgentConfig`` parent must yield a ``ClaudeAgentConfig`` (with the
   subclass-only fields coming from the *parent*, since the child never set them).

3. **The settings patch combine is ``merge(...)[0]`` rather than
   ``combine_patches(...)`` directly.** These are value-identical (``merge`` *is*
   ``combine_patches`` plus a discarded narrowing list -- the ``[0]`` keeps only the
   combined patch), so the same ``__extend``-over-``__extend`` overlay marking the
   ``AgentTypeConfig`` prototype uses reproduces it. The parent contributes the base
   patch (``parent_values.get(field) or {}``) and the child the higher patch.

Everything else is the same ``serialize -> pre-process -> overlay-merge -> reparse``
pipeline: bare fields assign-by-set (child wins, parent carries through), settings
patches accumulate, ``lower`` (not ``finalize``) preserves the inner settings
markers, and the merged dict re-parses into ``type(parent_config)``.

This is exploratory, additive code. It never calls
``_apply_custom_overrides_to_parent_config``, ``merge``, ``merge_with``, or
``combine_patches`` (that would make the property test tautological); it reproduces
the result purely through ``model_dump`` -> overlay node algebra -> ``model_validate``.
"""

from typing import Any

from imbue.mngr.config import agent_config_registry
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import is_settings_patch_field
from imbue.overlay.node_merge import combine
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.operators import EXTEND_SUFFIX

# The routing-metadata fields that ``_apply_custom_overrides_to_parent_config``
# skips (it iterates ``model_fields_set - _METADATA_FIELDS``). Read off the
# production module as a module attribute (not imported as an underscore name) so
# the dependency on the private constant is explicit and visible at its use sites,
# and the prototype stays in exact lockstep with the function it reproduces -- if a
# field were added to the skip set, the prototype follows automatically rather than
# silently diverging. Mirrors how ``overlay_merge_mngr_prototype.py`` reads
# ``data_types._CONTAINER_DICT_FIELDS``.
_METADATA_FIELDS = agent_config_registry._METADATA_FIELDS


def _settings_patch_field_names(config_class: type[AgentTypeConfig]) -> frozenset[str]:
    """Return the names of every ``SettingsPatchField``-marked field on the class.

    These are the fields that ``_apply_custom_overrides_to_parent_config``
    accumulates via ``merge`` (e.g. ``ClaudeAgentConfig.settings_overrides``)
    rather than assigning by default. The marker is read off the field's pydantic
    metadata exactly as the production function reads it, so the prototype stays in
    lockstep without hard-coding any field name.
    """
    return frozenset(
        name for name, field in config_class.model_fields.items() if is_settings_patch_field(field.metadata)
    )


def _to_operator_dict(
    values: dict[str, Any],
    patch_field_names: frozenset[str],
    *,
    drop_metadata: bool,
) -> dict[str, Any]:
    """Pre-process a serialized layer dict into the overlay operator language.

    - ``drop_metadata`` (the override / custom side): every ``_METADATA_FIELDS``
      key (``parent_type`` / ``plugin``) is dropped, reproducing the
      ``field_name in _METADATA_FIELDS: continue`` skip in
      ``_apply_custom_overrides_to_parent_config``. They are inheritance routing, so
      the child's values for them must not land on the parent's config. The parent
      (base) side keeps its own metadata (it carries through unchanged for any field
      the child does not set, exactly as ``model_copy_update`` on the parent would).
    - Every ``SettingsPatchField`` key is renamed ``<field>__extend`` so overlay
      treats it as an ``Extend`` node and *accumulates* across layers (the
      ``merge`` / ``combine_patches`` branch of the production function), preserving
      any ``key__extend`` / ``key__assign`` markers nested inside the patch value.
    - Every other key is left bare -> a ``Default`` node (assign-by-default). With an
      ``exclude_unset`` sparse override dict, bare keys give the "child's set fields
      win, parent fields carry through" semantics directly.
    """
    result: dict[str, Any] = {}
    for key, value in values.items():
        if drop_metadata and key in _METADATA_FIELDS:
            continue
        if key in patch_field_names:
            result[f"{key}{EXTEND_SUFFIX}"] = value
        else:
            result[key] = value
    return result


def _from_operator_dict(merged: dict[str, Any], patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Invert ``_to_operator_dict``: strip the synthetic ``__extend`` suffix off the
    settings-patch fields so the merged dict re-parses against the real field names.

    Only the suffix this prototype added (for a ``SettingsPatchField`` name) is
    stripped; any genuine ``__extend`` marker still living *inside* a settings-patch
    value is left exactly as the algebra produced it, so the accumulated markers
    survive into the re-parsed model just as the production function leaves them in
    ``settings_overrides``.
    """
    result: dict[str, Any] = {}
    for key, value in merged.items():
        if key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in patch_field_names:
            result[key[: -len(EXTEND_SUFFIX)]] = value
        else:
            result[key] = value
    return result


def apply_custom_overrides_via_overlay(
    parent_config: AgentTypeConfig,
    custom_config: AgentTypeConfig,
) -> AgentTypeConfig:
    """Reproduce ``_apply_custom_overrides_to_parent_config(parent_config, custom_config)``
    via the overlay node algebra, never calling the production function (nor
    ``merge`` / ``merge_with`` / ``combine_patches``).

    Pipeline (see ``specs/whole-config-overlay-integration.md`` and the two prior
    prototypes):

    1. **Serialize.** ``parent_config.model_dump()`` (full -- the base the child
       folds onto) and ``custom_config.model_dump(exclude_unset=True)`` (sparse --
       only the fields the child wrote, the ``model_fields_set`` semantics the
       production function iterates). ``serialize_as_any=True`` so a subclass parent
       or custom serializes through its concrete type and its subclass-only fields
       survive (matching the ``MngrConfig`` prototype's container-entry finding);
       python mode, the same mode the function dumps in.
    2. **Pre-process.** Drop ``_METADATA_FIELDS`` from the child's dump (the
       ``parent_type`` / ``plugin`` skip); mark every ``SettingsPatchField``
       ``__extend`` on **both** sides (accumulate); everything else bare
       (assign-by-set).
    3. **Merge.** ``lift`` both pre-processed dicts and ``combine`` the child
       (higher) over the parent (lower). ``combine`` (not ``merge_narrowing_allowed``)
       because the production function reproduces only the merged *value* -- it
       discards the settings combine's narrowings (the ``[0]``) and performs no other
       narrowing.
    4. **Lower + re-parse.** ``lower`` the combined patch (preserving the inner
       settings ``__extend`` markers -- ``finalize`` would over-resolve them, the key
       discovery of the first prototype), strip the synthetic suffix, and re-parse
       into **``type(parent_config)``** via ``model_validate``. This is the
       class-switching crux: the output class follows the *parent*, so a base-class
       child folded onto a ``ClaudeAgentConfig`` parent yields a ``ClaudeAgentConfig``
       (its subclass-only fields supplied by the parent, since the child never set
       them).
    """
    config_class = type(parent_config)
    patch_field_names = _settings_patch_field_names(config_class)

    parent_full = parent_config.model_dump(serialize_as_any=True)
    custom_sparse = custom_config.model_dump(exclude_unset=True, serialize_as_any=True)

    lower_patch = lift(_to_operator_dict(parent_full, patch_field_names, drop_metadata=False))
    higher_patch = lift(_to_operator_dict(custom_sparse, patch_field_names, drop_metadata=True))
    merged_patch = combine(lower_patch, higher_patch)

    merged_dict = _from_operator_dict(lower(merged_patch), patch_field_names)
    return config_class.model_validate(merged_dict)
