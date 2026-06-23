"""Production overlay-merge pipeline for pydantic config models.

``merge_models_via_overlay`` reproduces a model's old field-by-field merge by going
through ``model_dump`` -> overlay ``combine`` -> ``model_validate``, built on the
typed-node algebra in ``imbue.overlay.node_merge``. It backs ``MngrConfig.merge_with`` and
``parent_type`` inheritance (``apply_parent_overrides``).

The pipeline is **serialize -> pre-process -> overlay merge -> reparse**:

1. Serialize the full base and the sparse (``exclude_unset``) override.
2. Pre-process each layer into the operator language by **one uniform recursive walk** of
   the live model and its dumped dict (``_to_operator_dict``). At every level the live
   value at each key selects the rule: a value that is a ``BaseModel`` -> ``<field>__extend``
   recursed into (field-by-field sub-model merge); a ``RegistryField`` dict -> a two-level
   ``__extend`` (the dict + each entry key) recursed per entry; a ``SettingsPatchField`` ->
   ``<field>__extend`` (accumulate, value as-is);
   ``drop_none_values`` -> drop keys that are ``None``; every other key stays bare (assign).
3. ``lift`` both and ``merge_narrowing_allowed`` override over base (the value, plus
   every narrowing path).
4. ``lower`` (not ``finalize``, so inner ``__extend`` markers survive), strip the
   synthetic suffixes via the symmetric ``_from_operator_dict``, reparse container
   entries into their concrete classes, and reparse the whole dict into ``type(base)``.

Sub-model fields are detected at *runtime*: a field's value is a sub-model iff the live
value is a ``BaseModel`` instance (a ``None`` is simply not a model; a discriminated-union
value's concrete class is its own ``type()``). ``RegistryField`` / ``SettingsPatchField``
marks are read directly off the live value's class (``field_markers``).

See ``config/README.md`` for the rationale behind each step (why ``exclude_unset``,
why a two-level container ``__extend``, why ``lower`` not ``finalize``, and how the
narrowing paths are routed). The per-function/helper docstrings below cover their
specific contracts.
"""

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import TypeVar

from pydantic import BaseModel

from imbue.mngr.config.field_markers import get_registry_field_names
from imbue.mngr.config.field_markers import get_settings_patch_field_names
from imbue.mngr.errors import ConfigParseError
from imbue.overlay.markers import StaticDict
from imbue.overlay.markers import StaticList
from imbue.overlay.markers import StaticTuple
from imbue.overlay.markers import is_static_marker
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.node_merge import merge_narrowing_allowed
from imbue.overlay.operators import EXTEND_SUFFIX
from imbue.overlay.operators import bare_key

ModelT = TypeVar("ModelT", bound=BaseModel)

# The ordered builtin-aggregate -> ``Static*`` marker mapping driving the override-side
# re-marking. Ordered tuple -> list -> dict so the ``isinstance`` probe is unambiguous (a
# ``StaticDict`` is both a dict and a ``Mapping``, but never a tuple/list).
_MARKER_BY_AGGREGATE_SHAPE: dict[type, Callable[[Any], Any]] = {
    tuple: StaticTuple,
    list: StaticList,
    dict: StaticDict,
}


def _submodel_values(model: BaseModel) -> dict[str, BaseModel]:
    """Map each field name of ``model`` whose live value is a ``BaseModel`` to that value.

    This is the runtime sub-model detection: a sub-model field is one whose *value* is a
    ``BaseModel`` instance (``None`` and scalars are excluded). ``dict(model)`` yields the
    ``{field: value}`` map without per-field ``getattr``."""
    return {name: value for name, value in dict(model).items() if isinstance(value, BaseModel)}


def _container_entry_models(
    model: BaseModel,
    registry_field_names: frozenset[str],
) -> dict[str, dict[Any, BaseModel]]:
    """For each ``RegistryField`` dict, map its keys to the live entry *model* stored
    there (so a container entry's own sub-model / settings-patch fields can be marked off
    a concrete instance, and so the merged entry can be re-parsed into ``type(entry_model)``
    -- recovering the concrete subclass rather than the declared base type)."""
    field_values = dict(model)
    models: dict[str, dict[Any, BaseModel]] = {}
    for field_name in registry_field_names:
        container = field_values.get(field_name) or {}
        models[field_name] = {key: value for key, value in container.items() if isinstance(value, BaseModel)}
    return models


def _to_operator_dict(
    values: dict[str, Any],
    live_model: BaseModel,
    *,
    drop_none_values: bool,
) -> dict[str, Any]:
    """Pre-process a serialized layer dict into the overlay operator language by one
    uniform recursive walk of ``live_model`` and its dumped ``values``.

    For each dumped key the live value selects the rule: a ``BaseModel`` value ->
    ``<field>__extend`` recursed into (field-by-field sub-model merge); a
    ``RegistryField`` field -> a two-level ``__extend`` (per-key deep merge, each entry
    recursed); a ``SettingsPatchField`` field -> ``<field>__extend`` (value as-is, so
    inner ``__extend`` / ``__assign`` markers survive and re-combine); every other key
    bare (assign-by-default). Under ``drop_none_values`` a ``None``-valued key is dropped
    (the "treat ``None`` as unset" rule, since the model's "unset" sentinel is ``None``).

    The same rule set applies at every depth: a sub-model may itself carry a registry /
    settings-patch field, and it is handled identically. (For the current config models a
    registry / settings-patch field only ever appears at a registry-entry top level, so the
    recursion is a no-op there -- behavior is unchanged, proven by the frozen guards.)
    """
    submodel_values = _submodel_values(live_model)
    registry_field_names = get_registry_field_names(type(live_model))
    settings_patch_field_names = get_settings_patch_field_names(type(live_model))
    entry_models_by_field = _container_entry_models(live_model, registry_field_names)
    result: dict[str, Any] = {}
    for key, value in values.items():
        if drop_none_values and value is None:
            continue
        if key in submodel_values and isinstance(value, dict):
            result[f"{key}{EXTEND_SUFFIX}"] = _to_operator_dict(
                value, submodel_values[key], drop_none_values=drop_none_values
            )
        elif key in registry_field_names and isinstance(value, dict):
            entry_models = entry_models_by_field.get(key, {})
            result[f"{key}{EXTEND_SUFFIX}"] = _container_to_operator_dict(
                value, entry_models, drop_none_values=drop_none_values
            )
        elif key in settings_patch_field_names:
            result[f"{key}{EXTEND_SUFFIX}"] = value
        else:
            result[key] = value
    return result


def _container_to_operator_dict(
    container: dict[Any, Any],
    entry_models: dict[Any, BaseModel],
    *,
    drop_none_values: bool,
) -> dict[Any, Any]:
    """Pre-process one already-serialized registry container (``{key: entry_dict}``): mark
    each entry **key** ``<key>__extend`` and recurse into the entry off its live model.

    Two levels of ``__extend`` reproduce the per-key container merge: the container field
    is ``__extend`` (the caller) so overlay merges *per key* (a key in one side carries
    through; a key in both recurses), and each entry key is *also* ``__extend`` so a key
    present in both layers ``combine``\\s the two entry patches (recursing into the entry's
    bare fields = assign-by-set, and its ``__extend``-marked settings patch = accumulate)
    rather than letting the higher entry assign-replace the lower one wholesale.

    An entry with no live model (should not happen for a serialized registry) is marked at
    the key level but not recursed.
    """
    result: dict[Any, Any] = {}
    for key, entry in container.items():
        entry_model = entry_models.get(key)
        if entry_model is not None and isinstance(entry, dict):
            marked = _to_operator_dict(entry, entry_model, drop_none_values=drop_none_values)
        else:
            marked = entry
        result[f"{key}{EXTEND_SUFFIX}"] = marked
    return result


def _from_operator_dict(
    merged: dict[str, Any],
    base_model: BaseModel | None,
    override_model: BaseModel | None = None,
) -> dict[str, Any]:
    """Invert ``_to_operator_dict``: strip the synthetic ``__extend`` suffix off the
    sub-model, registry, and settings-patch fields (and, two levels deep, each registry
    entry key), recursing so the merged dict re-parses against the real field names.
    Genuine ``__extend`` markers living *inside* a settings patch are left untouched.

    The field-name marks (registry / settings-patch) come from the class. Sub-model fields
    are detected at runtime off the live model: a field is a sub-model iff the base (or,
    when base lacks it, the override) live value is a ``BaseModel`` -- so a nullable
    sub-model that only the override set is still unmarked. A ``None`` ``base_model`` and
    ``override_model`` (no live instance available) leaves the dict untouched.
    """
    live_model = base_model if base_model is not None else override_model
    if live_model is None:
        return merged
    base_submodels = _submodel_values(base_model) if base_model is not None else {}
    override_submodels = _submodel_values(override_model) if override_model is not None else {}
    registry_field_names = get_registry_field_names(type(live_model))
    settings_patch_field_names = get_settings_patch_field_names(type(live_model))
    base_entry_models = _container_entry_models(base_model, registry_field_names) if base_model is not None else {}
    override_entry_models = (
        _container_entry_models(override_model, registry_field_names) if override_model is not None else {}
    )
    result: dict[str, Any] = {}
    for key, value in merged.items():
        is_extended = key.endswith(EXTEND_SUFFIX)
        unsuffixed = bare_key(key) if is_extended else key
        if is_extended and unsuffixed in registry_field_names and isinstance(value, dict):
            result[unsuffixed] = _container_from_operator_dict(
                value, base_entry_models.get(unsuffixed, {}), override_entry_models.get(unsuffixed, {})
            )
        elif is_extended and unsuffixed in settings_patch_field_names:
            result[unsuffixed] = value
        elif (
            is_extended
            and (unsuffixed in base_submodels or unsuffixed in override_submodels)
            and isinstance(value, dict)
        ):
            result[unsuffixed] = _from_operator_dict(
                value, base_submodels.get(unsuffixed), override_submodels.get(unsuffixed)
            )
        else:
            result[key] = value
    return result


def _container_from_operator_dict(
    container: dict[Any, Any],
    base_entry_models: dict[Any, BaseModel],
    override_entry_models: dict[Any, BaseModel],
) -> dict[Any, Any]:
    """Invert ``_container_to_operator_dict``: strip the ``__extend`` suffix off each entry
    key and recurse into the entry off its live model (base preferred, override fallback),
    so the merged container re-parses against the real entry keys and field names."""
    result: dict[Any, Any] = {}
    for marked_entry_key, entry in container.items():
        entry_key = bare_key(marked_entry_key)
        result[entry_key] = _from_operator_dict(
            entry, base_entry_models.get(entry_key), override_entry_models.get(entry_key)
        )
    return result


def _reparse_container_entries(
    merged_dict: dict[str, Any],
    registry_field_names: frozenset[str],
    entry_models_by_field: dict[str, dict[Any, BaseModel]],
) -> dict[str, Any]:
    """Re-parse each registry entry back into its concrete model class, returning a
    new dict with the registry fields replaced (the input is left untouched).

    The whole-model ``model_validate`` cannot pick a subclass for a registry entry
    (the declared value type is the base ``AgentTypeConfig`` / ``PluginConfig`` / ...),
    so a ``ClaudeAgentConfig`` entry would lose its subclass-only fields. Reproduce the
    per-key merge's class handling by re-parsing each entry dict into the concrete class
    of the live entry model (base-preferred, override fallback -- the table in
    ``entry_models_by_field``), exactly the class the per-key merge keeps for a key in
    both layers, or the override's class for a key only the override added.
    """
    result = dict(merged_dict)
    for field_name in registry_field_names:
        container = result.get(field_name)
        if not container:
            continue
        entry_models = entry_models_by_field.get(field_name, {})
        reparsed: dict[Any, Any] = {}
        for key, entry in container.items():
            entry_model = entry_models.get(key)
            if entry_model is None:
                raise ConfigParseError(f"no class recovered for {field_name}.{key}")
            reparsed[key] = type(entry_model).model_validate(entry)
        result[field_name] = reparsed
    return result


def _collect_static_marker_paths(model: BaseModel) -> set[tuple[str, ...]]:
    """Collect the dotted paths (as segment tuples) of every ``Static*`` marker value
    living on the *live* ``model``, matching the sparse ``model_dump(exclude_unset=True)``.

    ``model_dump`` strips ``Static*`` subclasses (``ScalarTuple`` / ``StaticList`` /
    ``StaticDict``) back to plain aggregates, so the overlay path would
    wrongly flag a higher-layer replacement of one (e.g. a string-shaped ``cli_args`` or a
    provider's ``allowed_ssh_cidrs``) as narrowing. Recording the marker paths here lets a
    consumer re-mark the dumped dict before pre-processing.
    """
    paths: set[tuple[str, ...]] = set()
    _walk_for_static_markers(model, (), paths)
    return paths


def _walk_for_static_markers(value: Any, path: tuple[str, ...], paths: set[tuple[str, ...]]) -> None:
    """Recurse ``value``, adding the path of every ``Static*`` marker leaf to ``paths``.

    Recurses into ``BaseModel`` sub-fields (via ``model_fields_set``, so it visits exactly
    the set fields the sparse ``model_dump(exclude_unset=True)`` carries) and into
    ``Mapping`` values (a container dict's entries, which are themselves models).
    ``is_static_marker`` is checked *before* ``Mapping`` because a ``StaticDict`` is both --
    it is an atomic leaf, not a container to recurse into. Keys are stringified to match
    ``model_dump``'s key serialization. ``dict(model)`` yields the ``{field: value}`` map
    without per-field ``getattr``; intersecting with ``model_fields_set`` keeps it sparse.
    """
    if is_static_marker(value):
        paths.add(path)
        return
    if isinstance(value, BaseModel):
        set_fields = value.model_fields_set
        for field_name, field_value in dict(value).items():
            if field_name in set_fields:
                _walk_for_static_markers(field_value, path + (field_name,), paths)
        return
    if isinstance(value, Mapping):
        for key, sub_value in value.items():
            _walk_for_static_markers(sub_value, path + (str(key),), paths)


def _remark_static_leaves(dumped: dict[str, Any], static_paths: set[tuple[str, ...]]) -> dict[str, Any]:
    """Re-wrap the leaf at each ``static_paths`` location of the freshly-dumped ``dumped``
    dict in the matching ``Static*`` marker (by shape: ``tuple`` -> ``StaticTuple``,
    ``list`` -> ``StaticList``, ``dict`` -> ``StaticDict``), so ``lift`` carries it through
    as an atomic, narrowing-exempt leaf.

    Mutates ``dumped`` in place (and returns it). The re-mark is a pure no-op round-trip
    (``StaticList(list(x)) == x``; see ``markers.py``), so it never changes the value, only
    its narrowing-exempt marking. A path whose intermediate segment is absent (the field
    was not in the sparse dump) is skipped defensively.
    """
    for path in static_paths:
        if not path:
            continue
        container: Any = dumped
        for segment in path[:-1]:
            if not isinstance(container, Mapping) or segment not in container:
                container = None
                break
            container = container[segment]
        if not isinstance(container, dict):
            continue
        leaf_key = path[-1]
        if leaf_key not in container:
            continue
        leaf = container[leaf_key]
        # Choose the marker by builtin-aggregate shape (ordered tuple -> list -> dict, so a
        # ``StaticDict``, which is also a dict, is unambiguous); non-aggregate leaves are left
        # untouched. ``isinstance`` matches even a leaf a previous re-mark wrapped in a
        # ``Static*`` subclass.
        for shape, marker in _MARKER_BY_AGGREGATE_SHAPE.items():
            if isinstance(leaf, shape):
                container[leaf_key] = marker(leaf)
                break
    return dumped


def merge_models_via_overlay(
    base: ModelT,
    override: BaseModel,
    *,
    serialize_as_any: bool = False,
    drop_none_values: bool = False,
) -> tuple[ModelT, list[str]]:
    """Merge ``override`` onto ``base``, returning the merged model and *every* narrowing
    path the overlay merge surfaced (see module docstring for the merge mechanics).

    Callers that only need the merged value drop the second element explicitly
    (e.g. ``apply_parent_overrides``).

    ``override`` must be the same class as ``base`` or a base class of it (the result
    reparses into ``type(base)``, so a sibling or more-derived ``override`` would silently
    lose fields); a mismatch raises ``ConfigParseError``.

    ``SettingsPatchField`` / ``RegistryField`` marks are read directly off each live
    model's class (``field_markers``): a settings-patch field accumulates via ``__extend``;
    a registry field (e.g. ``MngrConfig.agent_types``) merges per key via a two-level
    ``__extend``, its entries re-parsed into their concrete (sub)classes. Sub-model fields
    (whose value is itself a ``BaseModel``, e.g. ``logging`` / ``retry`` or a provider's
    ``security_group``) are detected at runtime and merged field-by-field (carrying a base's
    unset sub-fields through).

    ``serialize_as_any`` is threaded to ``model_dump`` so subclass entries serialize through
    their concrete type. ``drop_none_values`` drops ``None``-valued keys from both layers
    (the top-level None-padding case). A caller that needs to exclude a field entirely (e.g.
    inheritance-routing metadata) strips it from the inputs first, rather than this function
    knowing field names.

    Returns ``(merged, narrowings)``: ``merged`` is a ``type(base)`` instance (so a
    subclass like ``ClaudeAgentConfig`` keeps its concrete class and subclass-only
    fields), and ``narrowings`` is the full list of dotted paths where a
    higher-precedence bare assign drops a non-empty aggregate a lower scope set --
    both ``SettingsPatchField`` drops (inside an accumulating settings patch) and
    ordinary assign-by-default field drops. The latter exempt ``Static*`` atomic
    aggregates (a string-shaped ``cli_args``, a provider's ``allowed_ssh_cidrs``, an
    explicit ``StaticList`` / ``StaticDict``) via the override-side re-marking
    (``_remark_static_leaves``), so a coherent scalar replacement is not flagged. This
    is the single config-load narrowing detector; the loader routes the whole list
    into its flag-gated narrowing error.
    """
    if not isinstance(base, type(override)):
        raise ConfigParseError(f"Cannot merge {type(base).__name__} with {type(override).__name__}")
    config_class = type(base)
    registry_field_names = get_registry_field_names(config_class)
    pipeline = _run_overlay_pipeline(
        base,
        override,
        serialize_as_any=serialize_as_any,
        drop_none_values=drop_none_values,
    )

    merged_dict = _from_operator_dict(lower(pipeline.merged_patch), base, override)
    merged_dict = _reparse_container_entries(merged_dict, registry_field_names, pipeline.merged_entry_models)
    return config_class.model_validate(merged_dict), pipeline.all_narrowings


class _OverlayPipelineResult(BaseModel):
    """The shared outputs of the serialize -> re-mark -> pre-process -> lift ->
    ``merge_narrowing_allowed`` pipeline, consumed by the public merge (which lowers
    ``merged_patch``, re-parses, and returns ``all_narrowings``)."""

    model_config = {"arbitrary_types_allowed": True}

    merged_patch: Any
    all_narrowings: list[str]
    merged_entry_models: dict[str, dict[Any, BaseModel]]


def _run_overlay_pipeline(
    base: BaseModel,
    override: BaseModel,
    *,
    serialize_as_any: bool,
    drop_none_values: bool,
) -> _OverlayPipelineResult:
    """Run the serialize -> re-mark -> pre-process -> ``lift`` -> ``merge_narrowing_allowed``
    pipeline and return the merged patch plus *all* narrowing paths (unfiltered) and the
    merged live-entry-model table needed to lower/reparse.

    The override sparse dump is re-marked (``_remark_static_leaves``) so the ``Static*``
    markers ``model_dump`` strips are restored as atomic leaves before pre-processing --
    only the override (higher) side needs it, since ``would_assignment_narrow`` exempts an
    assignment when the *override* value is a static marker (the base side carries no
    weight in that exemption).
    """
    registry_field_names = get_registry_field_names(type(base))
    base_entry_models = _container_entry_models(base, registry_field_names)
    override_entry_models = _container_entry_models(override, registry_field_names)
    # A live-entry-model table spanning both layers (base preferred), so the unmark and
    # reparse steps recover a merged entry's class / sub-model fields regardless of which
    # side contributed the entry.
    merged_entry_models: dict[str, dict[Any, BaseModel]] = {
        field_name: {**override_entry_models.get(field_name, {}), **base_entry_models.get(field_name, {})}
        for field_name in registry_field_names
    }

    base_full = base.model_dump(serialize_as_any=serialize_as_any)
    override_sparse = override.model_dump(exclude_unset=True, serialize_as_any=serialize_as_any)
    # Restore the ``Static*`` markers that ``model_dump`` stripped from the override, so a
    # higher-layer replacement of an atomic aggregate (a string-shaped ``cli_args``, a
    # provider's ``allowed_ssh_cidrs``, etc.) is correctly exempt from narrowing.
    override_sparse = _remark_static_leaves(override_sparse, _collect_static_marker_paths(override))

    lower_patch = lift(
        _to_operator_dict(
            base_full,
            base,
            # When ``drop_none_values`` is set, ``None`` is the model's "unset" sentinel on
            # *both* sides (TOML has no null), so a ``None`` in the base dump is dropped too.
            # This treats a ``None``-valued base field (e.g. a ``model_construct``'d
            # accumulator whose ``retry`` / ``logging`` sub-model is ``None``) as unset,
            # reproducing the old merge's defensive None-base guard: the re-parse then either
            # carries the override's value or applies the field default, rather than feeding a
            # ``None`` into a non-nullable field. For the loader's real accumulator the only
            # base ``None`` fields are the nullable ``pager`` / ``connect_command`` (whose
            # default is also ``None``), so this is behavior-identical there.
            drop_none_values=drop_none_values,
        )
    )
    higher_patch = lift(
        _to_operator_dict(
            override_sparse,
            override,
            drop_none_values=drop_none_values,
        )
    )
    # ``merge_narrowing_allowed`` (not ``combine``) so the merge also returns the
    # narrowing paths without raising.
    merged_patch, all_narrowings = merge_narrowing_allowed(lower_patch, higher_patch)
    return _OverlayPipelineResult(
        merged_patch=merged_patch,
        all_narrowings=all_narrowings,
        merged_entry_models=merged_entry_models,
    )


def _extend_example(dotted_path: str) -> str:
    """Render an ``__extend`` snippet for a dotted key path, for the narrowing remediation.

    The suffix goes on the *top* key of the layer, and each nested segment is itself
    ``__extend``-ed so deeper levels keep merging rather than replacing: ``permissions.allow``
    -> ``permissions__extend = {allow__extend = ...}`` and a top-level ``work_dir_extra_paths``
    -> ``work_dir_extra_paths__extend = ...``. The value is a ``...`` placeholder (the key,
    not the value, is what the user needs to get right).
    """
    segments = dotted_path.split(".")
    expr = "..."
    for index in range(len(segments) - 1, -1, -1):
        is_innermost = index == len(segments) - 1
        expr = f"{segments[index]}__extend = {expr}" if is_innermost else f"{segments[index]}__extend = {{{expr}}}"
    return expr


def suffix_remediation(example_key_path: str | None = None) -> str:
    """Render the ``__extend`` / ``__assign`` suffix remediation for the config-load surface.

    ``example_key_path`` (a narrowed dotted path) tailors the example to the user's actual key.
    The externally-owned ``settings_overrides`` surface uses ``__mngr_merge`` instead (see
    ``external_settings``); only mngr's own config uses these suffixes.
    """
    example = (
        _extend_example(example_key_path) if example_key_path else 'permissions__extend = {allow__extend = ["..."]}'
    )
    return (
        "To keep the additive behavior for a specific key, use the `__extend` suffix on the "
        f"key in the higher-precedence layer (e.g. `{example}`), "
        "or `__assign` to replace it intentionally without this error."
    )


def build_settings_narrowing_message(detail_lines: Sequence[str], *, remediation: str) -> str:
    """Build the user-facing settings-narrowing error body shared by config-load and
    provisioning.

    ``detail_lines`` describe what narrowed (the config loader names the assigning and
    dropped-from scopes per key; the provision fold lists the dotted key paths). The preamble
    and the ``allow_settings_key_assignment_narrowing`` escape hatch are identical in both
    contexts; the per-key ``remediation`` differs by surface and is rendered by the caller
    (``suffix_remediation`` for config-load; the ``__mngr_merge`` remediation in
    ``external_settings`` for the externally-owned ``settings_overrides`` path).
    """
    return (
        "Settings narrowing detected: a higher-precedence settings layer would assign over "
        "a non-empty list/tuple/dict/set value from a lower-precedence layer, silently "
        "dropping the earlier entries.\n" + "\n".join(detail_lines) + "\n"
        "To opt into this assign-by-default behavior (and silence this error), set "
        "`allow_settings_key_assignment_narrowing = true` in your mngr config "
        "(or MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true).\n" + remediation
    )
