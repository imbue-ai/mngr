"""Production overlay-merge pipeline for pydantic config models.

``merge_models_via_overlay`` reproduces a model's old field-by-field merge by going
through ``model_dump`` -> overlay ``combine`` -> ``model_validate``, built on the
typed-node algebra in ``imbue.overlay.node_merge``. It backs ``AgentTypeConfig.merge_with``,
``MngrConfig.merge_with``, and ``parent_type`` inheritance.

The pipeline is **serialize -> pre-process -> overlay merge -> reparse**:

1. Serialize the full base and the sparse (``exclude_unset``) override.
2. Pre-process the override into the operator language: a ``SettingsPatchField`` ->
   ``<field>__extend`` (accumulate); each container-additive field -> a two-level
   ``__extend``; ``drop_none_values`` -> drop keys that are ``None`` on both sides
   (an unset scalar); every other key stays bare (assign).
3. ``lift`` both and ``merge_narrowing_allowed`` override over base (the value, plus
   the ``SettingsPatchField``-rooted narrowing paths for the with-narrowings variant).
4. ``lower`` (not ``finalize``, so inner ``__extend`` markers survive), strip the
   synthetic suffixes, reparse container entries into their concrete classes, and
   reparse the whole dict into ``type(base)``.

See ``config/README.md`` for the rationale behind each step (why ``exclude_unset``,
why a two-level container ``__extend``, why ``lower`` not ``finalize``, and how the
narrowing paths are routed). The per-function/helper docstrings below cover their
specific contracts.
"""

from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import TypeVar

from pydantic import BaseModel

from imbue.overlay.markers import StaticDict
from imbue.overlay.markers import StaticList
from imbue.overlay.markers import StaticTuple
from imbue.overlay.markers import is_static_marker
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.node_merge import merge_narrowing_allowed
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
) -> dict[str, Any]:
    """Re-parse each container entry back into its concrete model class, returning a
    new dict with the container fields replaced (the input is left untouched).

    The whole-model ``model_validate`` cannot pick a subclass for a container entry
    (the declared value type is the base ``AgentTypeConfig`` / ``PluginConfig`` /
    ...), so a ``ClaudeAgentConfig`` entry would lose its subclass-only fields.
    Reproduce the per-key merge's class handling by re-parsing each entry dict into
    the class it came from: prefer the base entry's class (the lower layer, which the
    per-key merge keeps for a key present in both), falling back to the override
    entry's class for a key the override added.
    """
    result = dict(merged_dict)
    for field_name in container_dict_field_names:
        container = result.get(field_name)
        if not container:
            continue
        base_field_classes = base_classes.get(field_name, {})
        override_field_classes = override_classes.get(field_name, {})
        reparsed: dict[Any, Any] = {}
        for key, entry in container.items():
            entry_class = base_field_classes.get(key) or override_field_classes.get(key)
            assert entry_class is not None, f"no class recovered for {field_name}.{key}"
            reparsed[key] = entry_class.model_validate(entry)
        result[field_name] = reparsed
    return result


def _is_settings_patch_narrowing(
    dotted_path: str,
    settings_patch_field_names: frozenset[str],
    container_dict_field_names: frozenset[str],
    entry_classes_by_field: dict[str, dict[Any, type[BaseModel]]],
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn,
) -> bool:
    """Return True if ``dotted_path`` is rooted at a ``SettingsPatchField`` field.

    The overlay narrowing paths come back with bare (un-suffixed) segment names
    because ``lift`` strips the ``__extend`` markers when building the patch. Two
    rooting shapes count as a settings-patch narrowing:

    - ``<settings_field>...`` -- the merged model's own ``SettingsPatchField`` field
      (the ``AgentTypeConfig`` case, where ``settings_overrides`` is top-level).
    - ``<container>.<entry>.<settings_field>...`` -- a ``SettingsPatchField`` field of
      a container entry's concrete class (the ``MngrConfig`` case, where the patch
      lives inside an ``agent_types`` entry).

    Every other narrowing path is a non-settings field, which the loader's
    ``detect_settings_narrowing`` already handles (and which exempts settings fields),
    so surfacing it here would double-count -- those are filtered out.
    """
    segments = dotted_path.split(".")
    if segments[0] in settings_patch_field_names:
        return True
    if (
        len(segments) >= 3
        and segments[0] in container_dict_field_names
        and segments[1] in entry_classes_by_field.get(segments[0], {})
    ):
        entry_class = entry_classes_by_field[segments[0]][segments[1]]
        return segments[2] in settings_patch_field_names_for_class(entry_class)
    return False


def _collect_static_marker_paths(model: BaseModel) -> set[tuple[str, ...]]:
    """Collect the dotted paths (as segment tuples) of every ``Static*`` marker value
    living on the *live* ``model``, matching the sparse ``model_dump(exclude_unset=True)``.

    ``model_dump`` strips ``Static*`` subclasses (``ScalarTuple`` / ``StringDerivedTuple``
    / ``StaticList`` / ``StaticDict``) back to plain aggregates, so the overlay path would
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
    markers_by_shape: dict[type, Callable[[Any], Any]] = {tuple: StaticTuple, list: StaticList, dict: StaticDict}
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
        marker = markers_by_shape.get(type(leaf)) or markers_by_shape.get(_aggregate_shape(leaf))
        if marker is not None:
            container[leaf_key] = marker(leaf)
    return dumped


def _aggregate_shape(value: Any) -> type | None:
    """Return the builtin aggregate base (``tuple`` / ``list`` / ``dict``) of ``value``,
    so a marker can be chosen even if a previous re-mark already wrapped the leaf in a
    ``Static*`` subclass. Returns ``None`` for a non-aggregate."""
    if isinstance(value, tuple):
        return tuple
    if isinstance(value, list):
        return list
    if isinstance(value, dict):
        return dict
    return None


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

    The value-only entry point: delegates to
    ``merge_models_via_overlay_with_narrowings`` and discards the narrowing paths.
    Used by callers that only need the merged value (e.g. ``AgentTypeConfig.merge_with``)."""
    merged, _narrowings = merge_models_via_overlay_with_narrowings(
        base,
        override,
        settings_patch_field_names=settings_patch_field_names,
        drop_field_names=drop_field_names,
        serialize_as_any=serialize_as_any,
        container_dict_field_names=container_dict_field_names,
        drop_none_values=drop_none_values,
        settings_patch_field_names_for_class=settings_patch_field_names_for_class,
    )
    return merged


def merge_models_via_overlay_with_narrowings(
    base: ModelT,
    override: BaseModel,
    *,
    settings_patch_field_names: frozenset[str],
    drop_field_names: frozenset[str] = frozenset(),
    serialize_as_any: bool = False,
    container_dict_field_names: frozenset[str] = frozenset(),
    drop_none_values: bool = False,
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn | None = None,
) -> tuple[ModelT, list[str]]:
    """Merge ``override`` onto ``base`` and also return the ``SettingsPatchField``
    narrowing paths (see module docstring for the merge mechanics).

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

    Returns ``(merged, narrowings)``: ``merged`` is a ``type(base)`` instance (so a
    subclass like ``ClaudeAgentConfig`` keeps its concrete class and subclass-only
    fields), and ``narrowings`` is the list of dotted paths where a higher-precedence
    bare assign drops a non-empty aggregate that a lower scope set *inside a*
    ``SettingsPatchField`` field. The narrowings are filtered to settings-patch fields
    only (see ``_is_settings_patch_narrowing``): non-settings narrowings are left for
    the loader's ``detect_settings_narrowing`` (which exempts settings fields), so
    surfacing them here would double-count and would mis-narrow scalar fields whose
    ``Static*`` markers do not survive the ``model_dump``.
    """
    config_class = type(base)
    pipeline = _run_overlay_pipeline(
        base,
        override,
        settings_patch_field_names=settings_patch_field_names,
        drop_field_names=drop_field_names,
        serialize_as_any=serialize_as_any,
        container_dict_field_names=container_dict_field_names,
        drop_none_values=drop_none_values,
        settings_patch_field_names_for_class=settings_patch_field_names_for_class,
    )
    settings_narrowings = [
        dotted_path
        for dotted_path in pipeline.all_narrowings
        if _is_settings_patch_narrowing(
            dotted_path,
            settings_patch_field_names,
            container_dict_field_names,
            pipeline.merged_classes,
            pipeline.entry_patch_fn,
        )
    ]

    merged_dict = _from_operator_dict(
        lower(pipeline.merged_patch),
        settings_patch_field_names,
        container_dict_field_names,
        pipeline.merged_classes,
        pipeline.entry_patch_fn,
    )
    merged_dict = _reparse_container_entries(
        merged_dict, container_dict_field_names, pipeline.base_classes, pipeline.override_classes
    )
    return config_class.model_validate(merged_dict), settings_narrowings


class _OverlayPipelineResult(BaseModel):
    """The shared outputs of the serialize -> re-mark -> pre-process -> lift ->
    ``merge_narrowing_allowed`` pipeline, consumed both by the value-producing public
    merge (which lowers ``merged_patch`` and re-parses) and by
    ``_overlay_all_narrowing_paths`` (which only reads ``all_narrowings``)."""

    model_config = {"arbitrary_types_allowed": True}

    merged_patch: Any
    all_narrowings: list[str]
    base_classes: dict[str, dict[Any, type[BaseModel]]]
    override_classes: dict[str, dict[Any, type[BaseModel]]]
    merged_classes: dict[str, dict[Any, type[BaseModel]]]
    entry_patch_fn: SettingsPatchFieldNamesFn


def _run_overlay_pipeline(
    base: BaseModel,
    override: BaseModel,
    *,
    settings_patch_field_names: frozenset[str],
    drop_field_names: frozenset[str],
    serialize_as_any: bool,
    container_dict_field_names: frozenset[str],
    drop_none_values: bool,
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn | None,
) -> _OverlayPipelineResult:
    """Run the serialize -> re-mark -> pre-process -> ``lift`` -> ``merge_narrowing_allowed``
    pipeline and return the merged patch plus *all* narrowing paths (unfiltered) and the
    class tables needed to lower/reparse.

    The override sparse dump is re-marked (``_remark_static_leaves``) so the ``Static*``
    markers ``model_dump`` strips are restored as atomic leaves before pre-processing --
    only the override (higher) side needs it, since ``would_assignment_narrow`` exempts an
    assignment when the *override* value is a static marker (the base side carries no
    weight in that exemption).
    """
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
    # Restore the ``Static*`` markers that ``model_dump`` stripped from the override, so a
    # higher-layer replacement of an atomic aggregate (a string-shaped ``cli_args``, a
    # provider's ``allowed_ssh_cidrs``, etc.) is correctly exempt from narrowing.
    override_sparse = _remark_static_leaves(override_sparse, _collect_static_marker_paths(override))

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
    # ``merge_narrowing_allowed`` (not ``combine``) so the merge also returns the
    # narrowing paths without raising.
    merged_patch, all_narrowings = merge_narrowing_allowed(lower_patch, higher_patch)
    return _OverlayPipelineResult(
        merged_patch=merged_patch,
        all_narrowings=all_narrowings,
        base_classes=base_classes,
        override_classes=override_classes,
        merged_classes=merged_classes,
        entry_patch_fn=entry_patch_fn,
    )


def _overlay_all_narrowing_paths(
    base: BaseModel,
    override: BaseModel,
    *,
    settings_patch_field_names: frozenset[str],
    drop_field_names: frozenset[str] = frozenset(),
    serialize_as_any: bool = False,
    container_dict_field_names: frozenset[str] = frozenset(),
    drop_none_values: bool = False,
    settings_patch_field_names_for_class: SettingsPatchFieldNamesFn | None = None,
) -> list[str]:
    """Return the FULL (unfiltered) list of overlay narrowing paths for ``override`` over
    ``base`` -- every dotted path where a higher-precedence bare assign drops a non-empty
    aggregate, with no ``SettingsPatchField`` filter applied.

    Runs the same serialize -> re-mark -> pre-process -> ``lift`` -> ``merge_narrowing_allowed``
    pipeline as ``merge_models_via_overlay_with_narrowings`` (sharing
    ``_run_overlay_pipeline``), but skips the settings-patch filter. Exposed for the
    migration's equivalence test against ``detect_settings_narrowing``; the public merge
    functions are unaffected.
    """
    pipeline = _run_overlay_pipeline(
        base,
        override,
        settings_patch_field_names=settings_patch_field_names,
        drop_field_names=drop_field_names,
        serialize_as_any=serialize_as_any,
        container_dict_field_names=container_dict_field_names,
        drop_none_values=drop_none_values,
        settings_patch_field_names_for_class=settings_patch_field_names_for_class,
    )
    return pipeline.all_narrowings
