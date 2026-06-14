"""Production overlay-merge pipeline for pydantic config models.

This module replaces the field-by-field pydantic copy in
``AgentTypeConfig.merge_with`` with a *serialize -> pre-process -> overlay-merge ->
reparse* pipeline built on the typed-node algebra in ``imbue.overlay.node_merge``.
It is the promotion of the proven ``overlay_merge_prototype.py`` proof-of-approach
(see ``specs/whole-config-overlay-integration.md``) into a reusable production
function, behavior-identical to the old merge it replaces.

The single public entry point ``merge_models_via_overlay`` reproduces the *result*
of the model's old field-by-field merge by going only through ``model_dump`` ->
overlay ``combine`` -> ``model_validate``:

1. **Serialize.** ``base.model_dump()`` (full -- the accumulated base) and
   ``override.model_dump(exclude_unset=True)`` (sparse -- only the fields this layer
   actually wrote, the ``model_fields_set`` semantics the model-level merge relies
   on). Python mode, the same mode the old ``merge_with`` dumps in, so values
   round-trip without json-mode coercion drift; the re-parse re-coerces declared
   types regardless.
2. **Pre-process.** Drop ``drop_field_names`` from the sparse override; rename each
   ``SettingsPatchField`` key (passed in as ``settings_patch_field_names``) to
   ``<field>__extend`` on **both** sides so the algebra accumulates it (``Extend``
   over ``Extend`` recurses, combining the two patches and any nested ``__extend``
   markers) -- exactly the ``combine_patches`` branch of the old merge. Every other
   key stays bare, which the algebra lifts to a ``Default`` (assign-by-default);
   combined with the sparse override dump this gives "override's set fields win,
   absent fields carry through" for free.
3. **Merge.** ``lift`` both pre-processed dicts and ``combine`` higher (override)
   over lower (base). ``combine`` (not ``merge_narrowing_allowed``) because this
   reproduces only the merged *value*; the old merge performs no narrowing.
4. **Lower + re-parse.** ``lower`` (not ``finalize``) the combined patch -- ``lower``
   preserves the accumulated inner ``__extend`` markers in the settings patch, just
   as the old merge stores the ``combine_patches`` output verbatim; ``finalize``
   over-resolves them and diverges. Strip the synthetic ``__extend`` suffix off the
   settings-patch fields, then re-parse into ``type(base)`` via ``model_validate``
   so a subclass (e.g. ``ClaudeAgentConfig``) stays its concrete class and its
   subclass-only fields round-trip.
"""

from typing import Any
from typing import TypeVar

from pydantic import BaseModel

from imbue.overlay.node_merge import combine
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.operators import EXTEND_SUFFIX

ModelT = TypeVar("ModelT", bound=BaseModel)


def _to_operator_dict(
    values: dict[str, Any],
    settings_patch_field_names: frozenset[str],
    drop_field_names: frozenset[str],
) -> dict[str, Any]:
    """Pre-process a serialized layer dict into the overlay operator language.

    ``drop_field_names`` keys are dropped (used to drop ``None``-padded or
    routing-metadata fields off the override side). Every
    ``settings_patch_field_names`` key is renamed ``<field>__extend`` so the overlay
    algebra treats it as an ``Extend`` node -- which makes it *accumulate* across
    layers (``Extend`` over ``Extend`` recurses, combining the two patches and any
    nested ``__extend`` markers), reproducing the ``combine_patches`` branch of the
    old merge. The value is left untouched, so any ``key__extend`` / ``key__assign``
    markers the layer already placed *inside* the settings patch survive and
    re-combine. Every other key is left bare -- a ``Default`` (assign-by-default).
    """
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key in drop_field_names:
            continue
        if key in settings_patch_field_names:
            result[f"{key}{EXTEND_SUFFIX}"] = value
        else:
            result[key] = value
    return result


def _from_operator_dict(merged: dict[str, Any], settings_patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Invert ``_to_operator_dict``: strip the synthetic ``__extend`` suffix off the
    settings-patch fields so the merged dict re-parses against the real field names.

    Only the suffix this pipeline itself added (for a ``SettingsPatchField`` name) is
    stripped; every other key -- including any genuine ``__extend`` marker still
    living *inside* a settings-patch value -- is left exactly as the algebra
    produced it, so the accumulated markers survive into the re-parsed model just as
    the old merge leaves them in the settings patch.
    """
    result: dict[str, Any] = {}
    for key, value in merged.items():
        if key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in settings_patch_field_names:
            result[key[: -len(EXTEND_SUFFIX)]] = value
        else:
            result[key] = value
    return result


def merge_models_via_overlay(
    base: ModelT,
    override: BaseModel,
    *,
    settings_patch_field_names: frozenset[str],
    drop_field_names: frozenset[str] = frozenset(),
    serialize_as_any: bool = False,
) -> ModelT:
    """Merge ``override`` onto ``base`` via the overlay node algebra (see module docstring).

    ``settings_patch_field_names`` are the ``SettingsPatchField``-marked field names
    (accumulate via ``__extend`` rather than assign-by-default). ``drop_field_names``
    are dropped from the sparse override dump before merging (e.g. routing metadata).
    ``serialize_as_any`` is threaded to ``model_dump`` so subclass entries serialize
    through their concrete type when needed (not required for the flat
    ``AgentTypeConfig`` slice; matches the proven prototype's default of ``False``).

    Returns a ``type(base)`` instance, so a subclass (``ClaudeAgentConfig``) stays
    its concrete class with subclass-only fields intact.
    """
    config_class = type(base)

    base_full = base.model_dump(serialize_as_any=serialize_as_any)
    override_sparse = override.model_dump(exclude_unset=True, serialize_as_any=serialize_as_any)

    lower_patch = lift(_to_operator_dict(base_full, settings_patch_field_names, drop_field_names))
    higher_patch = lift(_to_operator_dict(override_sparse, settings_patch_field_names, drop_field_names))
    merged_patch = combine(lower_patch, higher_patch)

    merged_dict = _from_operator_dict(lower(merged_patch), settings_patch_field_names)
    return config_class.model_validate(merged_dict)
