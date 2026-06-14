"""PROTOTYPE -- not wired into any production code path.

A proof-of-approach for the spec in
``specs/whole-config-overlay-integration.md``: it demonstrates that
``AgentTypeConfig.merge_with`` (the cross-config-scope per-field merge) can be
reproduced by a *serialize -> pre-process -> overlay-merge -> reparse* pipeline
built entirely on the typed-node algebra in ``imbue.overlay.node_merge``, with no
field-by-field pydantic copy.

The single public entry point ``merge_agent_type_via_overlay`` reproduces the
*result* of ``base.merge_with(override)`` **without calling ``merge_with``** -- it
goes only through ``model_dump`` -> overlay ``combine`` -> ``model_validate``. The
matching property test (``overlay_merge_prototype_test.py``) asserts the two are
equal over a diverse corpus.

The findings (what pre-processing each field kind needs, where the naive pipeline
diverges, and how the divergences resolve) are written up in the PR report, not
here, to keep this file focused on the working pipeline.

This is exploratory code. It deliberately handles only the ``AgentTypeConfig``
family (including the ``ClaudeAgentConfig`` subclass), not the full
``MngrConfig`` tree, and it does *not* handle the ``parent_type`` /
``_apply_custom_overrides_to_parent_config`` class-switching variant.
"""

from typing import Any

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import is_settings_patch_field
from imbue.overlay.node_merge import combine
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.operators import EXTEND_SUFFIX


def _settings_patch_field_names(config_class: type[AgentTypeConfig]) -> frozenset[str]:
    """Return the names of every ``SettingsPatchField``-marked field on the class.

    These are the fields that ``merge_with`` accumulates via ``combine_patches``
    (e.g. ``ClaudeAgentConfig.settings_overrides``) rather than assigning by
    default. The marker is read off the field's pydantic metadata exactly as
    ``merge_with`` reads it, so the prototype stays in lockstep with the
    production rule without hard-coding any field name.
    """
    return frozenset(
        name for name, field in config_class.model_fields.items() if is_settings_patch_field(field.metadata)
    )


def _to_operator_dict(values: dict[str, Any], patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Pre-process a serialized layer dict into the overlay operator language.

    Every ``SettingsPatchField`` key is renamed to ``<field>__extend`` so the
    overlay algebra treats it as an ``Extend`` node -- which makes it *accumulate*
    across layers (``Extend`` over ``Extend`` recurses, combining the two patches
    and any nested ``__extend`` markers), exactly reproducing the
    ``combine_patches`` branch of ``merge_with``. Crucially the value is left
    untouched, so any ``key__extend`` / ``key__assign`` markers the layer already
    placed *inside* ``settings_overrides`` at config-load time are preserved and
    re-combined.

    Every other key is left bare, which the algebra lifts to a ``Default``
    (assign-by-default, narrowing-checked). Combined with an ``exclude_unset``
    sparse override dict, bare keys already give the "override's set fields win,
    absent fields carry through" semantics of the model-level merge -- no extra
    marking needed.
    """
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key in patch_field_names:
            result[f"{key}{EXTEND_SUFFIX}"] = value
        else:
            result[key] = value
    return result


def _from_operator_dict(merged: dict[str, Any], patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Invert ``_to_operator_dict``: strip the synthetic ``__extend`` suffix off the
    settings-patch fields so the merged dict re-parses against the real field names.

    Only the suffix this prototype itself added (for a ``SettingsPatchField`` name)
    is stripped; every other key -- including any genuine ``__extend`` marker still
    living *inside* a settings-patch value -- is left exactly as the algebra
    produced it, so the accumulated markers survive into the re-parsed model just as
    ``merge_with`` leaves them in ``settings_overrides``.
    """
    result: dict[str, Any] = {}
    for key, value in merged.items():
        if key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in patch_field_names:
            result[key[: -len(EXTEND_SUFFIX)]] = value
        else:
            result[key] = value
    return result


def merge_agent_type_via_overlay(base: AgentTypeConfig, override: AgentTypeConfig) -> AgentTypeConfig:
    """Reproduce ``base.merge_with(override)`` via the overlay node algebra.

    Pipeline (see ``specs/whole-config-overlay-integration.md``):

    1. **Serialize.** ``override.model_dump(exclude_unset=True)`` -- the sparse set
       of fields this layer actually wrote (the ``model_fields_set`` semantics the
       model-level merge relies on) -- and ``base.model_dump()`` -- the full
       accumulated base. Both in **python** mode (the same mode ``merge_with`` uses
       internally), so values round-trip without json-mode coercion drift; the
       re-parse step re-coerces declared types regardless.
    2. **Pre-process** each dict into the operator language: settings-patch fields
       become ``__extend`` (accumulate), everything else stays bare (assign-by-set).
    3. **Merge** with the node algebra: ``lift`` both pre-processed dicts to node
       patches and ``combine`` higher (override) over lower (base). ``combine`` is
       used directly (not ``merge_narrowing_allowed``) because this prototype
       reproduces only the merged *value*; narrowing detection is a separate,
       non-value concern that ``merge_with`` itself does not perform.
    4. **Lower** the combined patch back to a suffix-keyed dict (preserving the
       accumulated inner ``__extend`` markers in the settings patch), invert the
       synthetic suffix, and **re-parse** into ``type(base)`` via ``model_validate``
       -- so a ``ClaudeAgentConfig`` stays a ``ClaudeAgentConfig`` and subclass-only
       fields (``auto_dismiss_dialogs`` etc.) round-trip. ``model_validate`` re-runs
       the field validators, restoring declared tuple types from the dumped values.

    This function never calls ``merge_with`` (that would make the property test
    tautological); it reproduces the result purely through dump -> overlay ->
    reparse.
    """
    config_class = type(base)
    patch_field_names = _settings_patch_field_names(config_class)

    base_full = base.model_dump()
    override_sparse = override.model_dump(exclude_unset=True)

    lower_patch = lift(_to_operator_dict(base_full, patch_field_names))
    higher_patch = lift(_to_operator_dict(override_sparse, patch_field_names))
    merged_patch = combine(lower_patch, higher_patch)

    merged_dict = _from_operator_dict(lower(merged_patch), patch_field_names)
    return config_class.model_validate(merged_dict)
