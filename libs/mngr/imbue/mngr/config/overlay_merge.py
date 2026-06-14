"""Production overlay-merge pipeline for pydantic config models.

This module replaces the field-by-field pydantic copy in
``AgentTypeConfig.merge_with`` *and* ``MngrConfig.merge_with`` with a
*serialize -> pre-process -> overlay-merge -> reparse* pipeline built on the
typed-node algebra in ``imbue.overlay.node_merge``. It is the promotion of the
proven ``AgentTypeConfig`` and ``MngrConfig`` proofs-of-approach (see
``specs/whole-config-overlay-integration.md``) into a reusable production function,
behavior-identical to the old merges it replaces.

The single public entry point ``merge_models_via_overlay`` reproduces the *result*
of the model's old field-by-field merge by going only through ``model_dump`` ->
overlay ``combine`` -> ``model_validate``:

1. **Serialize.** ``base.model_dump()`` (full -- the accumulated base) and
   ``override.model_dump(exclude_unset=True)`` (sparse -- only the fields this layer
   actually wrote, the ``model_fields_set`` semantics the model-level merge relies
   on). Python mode, the same mode the old ``merge_with`` dumps in, so values
   round-trip without json-mode coercion drift; the re-parse re-coerces declared
   types regardless. ``serialize_as_any`` is threaded so subclass container entries
   (e.g. ``ClaudeAgentConfig``) serialize through their concrete type and keep their
   subclass-only fields.
2. **Pre-process.** Drop ``drop_field_names`` from the sparse override; when
   ``drop_none_values`` is set, also drop keys whose value is ``None`` on *both*
   sides (``parse_config`` pads every unset scalar to ``None``, and TOML has no null,
   so a ``None`` value is always *unset*; dropping it reproduces ``_assign_scalar`` /
   the ``if override.<field> is not None`` guards -- and dropping a ``None`` base
   sub-model reproduces the old merge's defensive None-base guard, so the re-parse
   defaults it rather than feeding ``None`` into a non-nullable field).
   Rename each ``SettingsPatchField`` key (passed in as
   ``settings_patch_field_names``) to ``<field>__extend`` so the algebra accumulates
   it (``Extend`` over ``Extend`` recurses, combining the two patches and any nested
   ``__extend`` markers) -- exactly the ``combine_patches`` branch of the old merge.
   Each *container-additive* field (``container_dict_field_names``) is rewritten as a
   two-level ``__extend``: the container field itself is ``__extend`` (so overlay
   deep-merges per key, never assign-replacing the whole dict), and each entry key is
   *also* ``__extend`` with the entry's own ``SettingsPatchField``\\s marked
   ``__extend`` (so a key present in both layers ``combine``\\s the two entry patches
   field-by-field -- which *is* the entry's own ``merge_with``). Every other key
   stays bare, which the algebra lifts to a ``Default`` (assign-by-default).
3. **Merge.** ``lift`` both pre-processed dicts and ``combine`` higher (override)
   over lower (base). ``combine`` (not ``merge_narrowing_allowed``) because this
   reproduces only the merged *value*; the old merge performs no narrowing.
4. **Lower + re-parse.** ``lower`` (not ``finalize``) the combined patch -- ``lower``
   preserves the accumulated inner ``__extend`` markers in the settings patch, just
   as the old merge stores the ``combine_patches`` output verbatim; ``finalize``
   over-resolves them and diverges. Strip the synthetic ``__extend`` suffixes off the
   settings-patch fields and container fields/entries, re-parse each container entry
   into its concrete (sub)class, then re-parse the whole dict into ``type(base)`` via
   ``model_validate`` so a subclass (e.g. ``ClaudeAgentConfig``) stays its concrete
   class and its subclass-only fields round-trip.
"""

from collections.abc import Callable
from typing import Any
from typing import TypeVar

from pydantic import BaseModel

from imbue.overlay.node_merge import combine
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.operators import EXTEND_SUFFIX

ModelT = TypeVar("ModelT", bound=BaseModel)

# A callable that returns the ``SettingsPatchField``-marked field names of a model
# class. Threaded in (rather than imported) so this module stays free of the
# ``data_types`` dependency it would otherwise import in a cycle: ``data_types``
# imports ``merge_models_via_overlay`` from here.
SettingsPatchFieldNamesFn = Callable[[type[BaseModel]], frozenset[str]]


def _mark_settings_fields(values: dict[str, Any], settings_patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Rename each ``SettingsPatchField`` key to ``<field>__extend`` so the overlay
    algebra accumulates it (the ``combine_patches`` branch), leaving every other key
    bare. The value is left untouched so any ``key__extend`` / ``key__assign``
    markers already living *inside* the settings patch survive and re-combine.
    """
    if not settings_patch_field_names:
        return values
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key in settings_patch_field_names:
            result[f"{key}{EXTEND_SUFFIX}"] = value
        else:
            result[key] = value
    return result


def _unmark_settings_fields(values: dict[str, Any], settings_patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Invert ``_mark_settings_fields``: strip only the synthetic ``__extend`` suffix
    this pipeline added for a ``SettingsPatchField`` name, leaving every other key --
    including any genuine ``__extend`` marker still living *inside* a settings-patch
    value -- exactly as the algebra produced it.
    """
    if not settings_patch_field_names:
        return values
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in settings_patch_field_names:
            result[key[: -len(EXTEND_SUFFIX)]] = value
        else:
            result[key] = value
    return result


def _container_entry_classes(
    model: BaseModel,
    container_dict_field_names: frozenset[str],
) -> dict[str, dict[Any, type[BaseModel]]]:
    """For each container-additive field, map its keys to the concrete model class of
    the entry stored there.

    The class is needed both to read its ``SettingsPatchField`` set (so a
    ``ClaudeAgentConfig`` entry's ``settings_overrides`` is marked ``__extend``) and
    so the merged entry can be re-parsed into the right subclass at the end. Read off
    the live model rather than the field annotation, so a concrete subclass entry is
    recovered rather than the declared base type. ``dict(model)`` yields the
    ``{field_name: value}`` map without ``getattr``.
    """
    field_values = dict(model)
    classes: dict[str, dict[Any, type[BaseModel]]] = {}
    for field_name in container_dict_field_names:
        container = field_values.get(field_name) or {}
        classes[field_name] = {key: type(value) for key, value in container.items()}
    return classes


def _preprocess_container(
    container: dict[Any, Any],
    entry_classes: dict[Any, type[BaseModel]],
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn,
) -> dict[Any, Any]:
    """Pre-process one already-serialized container dict (``{key: entry_dict}``): mark
    each entry **key** ``<key>__extend`` and mark each entry's settings-patch fields
    ``__extend``.

    Two levels of ``__extend`` are needed to reproduce the per-key container merge:
    the container field is ``__extend`` (caller) so overlay merges *per key* (key in
    one side carries through; key in both recurses) rather than assign-replacing the
    whole dict, and each entry key is *also* ``__extend`` so a key present in both
    layers ``combine``\\s the two entry patches (recursing into the entry's bare
    fields = assign-by-set, and its ``__extend``-marked settings patch = accumulate)
    rather than letting the higher entry assign-replace the lower one wholesale.
    """
    result: dict[Any, Any] = {}
    for key, entry in container.items():
        entry_class = entry_classes.get(key)
        patch_field_names = (
            settings_patch_field_names_for_class(entry_class) if entry_class is not None else frozenset()
        )
        result[f"{key}{EXTEND_SUFFIX}"] = _mark_settings_fields(entry, patch_field_names)
    return result


def _to_operator_dict(
    values: dict[str, Any],
    settings_patch_field_names: frozenset[str],
    drop_field_names: frozenset[str],
    container_dict_field_names: frozenset[str],
    entry_classes_by_field: dict[str, dict[Any, type[BaseModel]]],
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn,
    *,
    drop_none_values: bool,
) -> dict[str, Any]:
    """Pre-process a serialized layer dict into the overlay operator language.

    ``drop_field_names`` keys are dropped (e.g. routing metadata). When
    ``drop_none_values`` is set, ``None``-valued keys are also dropped (the override
    side's "treat ``None`` as unset" reproduction of ``_assign_scalar``). Each
    ``settings_patch_field_names`` key is renamed ``<field>__extend`` (accumulate);
    each ``container_dict_field_names`` key is rewritten as a two-level ``__extend``
    (per-key deep merge, see ``_preprocess_container``). Every other key is left bare
    (assign-by-default).
    """
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key in drop_field_names:
            continue
        if drop_none_values and value is None:
            continue
        if key in container_dict_field_names:
            entry_classes = entry_classes_by_field.get(key, {})
            result[f"{key}{EXTEND_SUFFIX}"] = _preprocess_container(
                value, entry_classes, settings_patch_field_names_for_class
            )
        elif key in settings_patch_field_names:
            result[f"{key}{EXTEND_SUFFIX}"] = value
        else:
            result[key] = value
    return result


def _from_operator_dict(
    merged: dict[str, Any],
    settings_patch_field_names: frozenset[str],
    container_dict_field_names: frozenset[str],
    entry_classes_by_field: dict[str, dict[Any, type[BaseModel]]],
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn,
) -> dict[str, Any]:
    """Invert ``_to_operator_dict``: strip the synthetic ``__extend`` suffix off the
    settings-patch fields, the container fields, and each container entry key/settings
    field, so the merged dict re-parses against the real field names. Genuine
    ``__extend`` markers living *inside* a settings patch are left untouched.
    """
    result: dict[str, Any] = {}
    for key, value in merged.items():
        if key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in container_dict_field_names:
            field_name = key[: -len(EXTEND_SUFFIX)]
            entry_classes = entry_classes_by_field.get(field_name, {})
            unmarked_container: dict[Any, Any] = {}
            for marked_entry_key, entry in value.items():
                entry_key = (
                    marked_entry_key[: -len(EXTEND_SUFFIX)]
                    if marked_entry_key.endswith(EXTEND_SUFFIX)
                    else marked_entry_key
                )
                entry_class = entry_classes.get(entry_key)
                patch_field_names = (
                    settings_patch_field_names_for_class(entry_class) if entry_class is not None else frozenset()
                )
                unmarked_container[entry_key] = _unmark_settings_fields(entry, patch_field_names)
            result[field_name] = unmarked_container
        elif key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in settings_patch_field_names:
            result[key[: -len(EXTEND_SUFFIX)]] = value
        else:
            result[key] = value
    return result


def _reparse_container_entries(
    merged_dict: dict[str, Any],
    container_dict_field_names: frozenset[str],
    base_classes: dict[str, dict[Any, type[BaseModel]]],
    override_classes: dict[str, dict[Any, type[BaseModel]]],
) -> None:
    """Re-parse each container entry back into its concrete model class, in place.

    The whole-model ``model_validate`` cannot pick a subclass for a container entry
    (the declared value type is the base ``AgentTypeConfig`` / ``PluginConfig`` /
    ...), so a ``ClaudeAgentConfig`` entry would lose its subclass-only fields.
    Reproduce the per-key merge's class handling by re-parsing each entry dict into
    the class it came from: prefer the base entry's class (the lower layer, which the
    per-key merge keeps for a key present in both), falling back to the override
    entry's class for a key the override added.
    """
    for field_name in container_dict_field_names:
        container = merged_dict.get(field_name)
        if not container:
            continue
        base_field_classes = base_classes.get(field_name, {})
        override_field_classes = override_classes.get(field_name, {})
        reparsed: dict[Any, Any] = {}
        for key, entry in container.items():
            entry_class = base_field_classes.get(key) or override_field_classes.get(key)
            assert entry_class is not None, f"no class recovered for {field_name}.{key}"
            reparsed[key] = entry_class.model_validate(entry)
        merged_dict[field_name] = reparsed


def merge_models_via_overlay(
    base: ModelT,
    override: BaseModel,
    *,
    settings_patch_field_names: frozenset[str],
    drop_field_names: frozenset[str] = frozenset(),
    serialize_as_any: bool = False,
    container_dict_field_names: frozenset[str] = frozenset(),
    drop_none_values: bool = False,
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn | None = None,
) -> ModelT:
    """Merge ``override`` onto ``base`` via the overlay node algebra (see module docstring).

    ``settings_patch_field_names`` are the ``SettingsPatchField``-marked field names
    on the *model itself* (accumulate via ``__extend`` rather than assign-by-default).
    ``drop_field_names`` are dropped from the sparse override dump before merging
    (e.g. routing metadata). ``serialize_as_any`` is threaded to ``model_dump`` so
    subclass entries serialize through their concrete type when needed.

    ``container_dict_field_names`` are container-additive dict fields (e.g.
    ``MngrConfig.agent_types``) merged per key via a two-level ``__extend``; their
    entries are re-parsed into their concrete (sub)classes. ``drop_none_values``
    drops ``None``-valued keys from the sparse override (the top-level None-padding
    case). ``settings_patch_field_names_for_class`` discovers the
    ``SettingsPatchField`` names of a *container entry* class; required whenever
    ``container_dict_field_names`` is non-empty.

    Returns a ``type(base)`` instance, so a subclass (``ClaudeAgentConfig``) stays its
    concrete class with subclass-only fields intact.
    """
    config_class = type(base)

    # Internal invariant (caller-side programming error, never a user/runtime
    # condition): a per-entry settings discoverer is required to pre-process container
    # entries. ``assert`` documents the contract without a user-facing exception.
    assert not container_dict_field_names or settings_patch_field_names_for_class is not None, (
        "settings_patch_field_names_for_class is required when container_dict_field_names is set"
    )
    # A no-op discoverer when there are no container fields: it is never consulted in
    # that case (no entry gets pre-processed), but typing wants a concrete callable.
    entry_patch_fn: SettingsPatchFieldNamesFn = settings_patch_field_names_for_class or (lambda _cls: frozenset())

    base_classes = _container_entry_classes(base, container_dict_field_names)
    override_classes = _container_entry_classes(override, container_dict_field_names)
    # A class table spanning both layers, so an entry present only in the override
    # (whose key is absent from ``base``) still has its settings fields marked.
    merged_classes: dict[str, dict[Any, type[BaseModel]]] = {
        field_name: {**base_classes.get(field_name, {}), **override_classes.get(field_name, {})}
        for field_name in container_dict_field_names
    }

    base_full = base.model_dump(serialize_as_any=serialize_as_any)
    override_sparse = override.model_dump(exclude_unset=True, serialize_as_any=serialize_as_any)

    lower_patch = lift(
        _to_operator_dict(
            base_full,
            settings_patch_field_names,
            drop_field_names,
            container_dict_field_names,
            merged_classes,
            entry_patch_fn,
            # When ``drop_none_values`` is set, ``None`` is the model's "unset"
            # sentinel on *both* sides (TOML has no null), so a ``None`` in the base
            # dump is dropped too. This treats a ``None``-valued base field (e.g. a
            # ``model_construct``'d accumulator whose ``retry`` / ``logging`` sub-model
            # is ``None``) as unset, reproducing the old merge's defensive None-base
            # guard: the re-parse then either carries the override's value or applies
            # the field default, rather than feeding a ``None`` into a non-nullable
            # field. For the loader's real accumulator the only base ``None`` fields
            # are the nullable ``pager`` / ``connect_command`` (whose default is also
            # ``None``), so this is behavior-identical there.
            drop_none_values=drop_none_values,
        )
    )
    higher_patch = lift(
        _to_operator_dict(
            override_sparse,
            settings_patch_field_names,
            drop_field_names,
            container_dict_field_names,
            merged_classes,
            entry_patch_fn,
            drop_none_values=drop_none_values,
        )
    )
    merged_patch = combine(lower_patch, higher_patch)

    merged_dict = _from_operator_dict(
        lower(merged_patch),
        settings_patch_field_names,
        container_dict_field_names,
        merged_classes,
        entry_patch_fn,
    )
    _reparse_container_entries(merged_dict, container_dict_field_names, base_classes, override_classes)
    return config_class.model_validate(merged_dict)
