"""PROTOTYPE -- not wired into any production code path.

Extends the ``AgentTypeConfig`` proof-of-approach (now promoted to the production
``overlay_merge.py`` and wired into ``AgentTypeConfig.merge_with``) up to the
**top-level** ``MngrConfig.merge_with``,
which is the real remaining risk for the design in
``specs/whole-config-overlay-integration.md``. It reproduces the *result* of
``base.merge_with(override)`` for a whole ``MngrConfig`` via the same
*serialize -> pre-process -> overlay-merge -> reparse* pipeline -- with **no**
field-by-field pydantic copy and **without** calling ``merge_with`` -- handling
the three things that distinguish the top-level merge from the sub-model slice:

1. **Top-level None-padding.** ``parse_config`` pads every scalar field to ``None``
   (``raw.pop(field, None)``), so ``override.model_dump(exclude_unset=True)``
   over-reports: all 24 fields appear, ``None`` for the ones the layer never wrote.
   TOML has no null, so a ``None`` scalar can only ever be the padding sentinel =
   *unset*. Dropping ``None``-valued keys from the override dict reproduces
   ``_assign_scalar`` ("override wins iff not None, else base") exactly, and also
   handles the ``retry`` / ``logging`` sub-models (default ``None`` when unset ->
   dropped -> base carried).

2. **Container-additive fields** (``agent_types``, ``providers``, ``plugins``,
   ``commands``, ``create_templates``): ``merge_with`` merges these per key via
   ``_merge_container_dict`` (key in both -> ``base[k].merge_with(override[k])``;
   key in one side -> carried through). Marking the container field ``__extend``
   makes overlay deep-merge per key; for a key present in both sides overlay
   ``combine``\\s the (full) base entry patch with the (sparse) override entry
   patch -- which *is* the per-entry ``merge_with`` -- provided each entry is itself
   pre-processed (its ``SettingsPatchField``\\s marked ``__extend``, just as the
   ``AgentTypeConfig`` prototype does). So the marking is **recursive**: container
   ``__extend`` plus per-entry settings-patch ``__extend``.

3. **Defaults timing.** ``merge_with`` leaves unset scalars ``None`` in the
   intermediate; defaults are applied only by the loader's *final*
   ``MngrConfig.model_validate(config_dict)``. ``finalize_like_loader`` reproduces
   that final step (drop the padded ``None`` scalars, then ``model_validate`` to
   apply defaults), so the comparison is made at the user-visible stage on **both**
   sides rather than against a half-built padded intermediate.

This is exploratory code. It deliberately does not handle the ``parent_type`` /
``_apply_custom_overrides_to_parent_config`` class-switching variant (the next
risk after this).
"""

from typing import Any

from pydantic import BaseModel

from imbue.mngr.config import data_types
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import is_settings_patch_field
from imbue.mngr.config.loader import parse_config
from imbue.overlay.node_merge import combine
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import lower
from imbue.overlay.operators import EXTEND_SUFFIX

# The set of container-additive fields on ``MngrConfig`` whose merge is per-key
# (``agent_types`` ...). This prototype must stay in exact lockstep with the
# production ``_merge_container_dict`` carveout, so it reads the canonical set off
# ``data_types`` rather than re-listing the names (which would silently drift if a
# container field were added). Accessed as a module attribute (not imported as an
# underscore name) so the dependency on the private constant is explicit and
# visible at its use sites.
_CONTAINER_DICT_FIELDS = data_types._CONTAINER_DICT_FIELDS

# The model classes whose instances appear as values of the container-additive
# dicts. Each is a ``FrozenModel`` whose own ``merge_with`` the per-key combine
# must reproduce; the only one of these (today) that carries a
# ``SettingsPatchField`` is an ``AgentTypeConfig`` subclass, but the pre-processing
# reads the marker generically off ``model_fields`` so no name is hard-coded.


def _settings_patch_field_names(model_class: type[BaseModel]) -> frozenset[str]:
    """Return the ``SettingsPatchField``-marked field names on a model class.

    Read off the pydantic field metadata exactly as ``merge_with`` /
    ``_merge_container_dict`` do (transitively, via each entry's ``merge_with``),
    so the prototype stays in lockstep with the production rule without hard-coding
    any field name. A class with no such fields (every container entry type except
    the ``AgentTypeConfig`` settings-bearing subclass) yields an empty set.
    """
    return frozenset(
        name for name, field in model_class.model_fields.items() if is_settings_patch_field(field.metadata)
    )


def _mark_entry_settings(entry: dict[str, Any], patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Rename each ``SettingsPatchField`` key in a container *entry* dict to
    ``<field>__extend`` so overlay accumulates it (the ``combine_patches`` branch of
    the entry's ``merge_with``), leaving every other key bare (assign-by-set).

    This is exactly the ``_to_operator_dict`` pass from the ``AgentTypeConfig``
    prototype, applied to a single container entry. The value is left untouched so
    any ``key__extend`` / ``key__assign`` markers the entry already carries *inside*
    its settings patch survive and re-combine.
    """
    if not patch_field_names:
        return entry
    result: dict[str, Any] = {}
    for key, value in entry.items():
        if key in patch_field_names:
            result[f"{key}{EXTEND_SUFFIX}"] = value
        else:
            result[key] = value
    return result


def _unmark_entry_settings(entry: dict[str, Any], patch_field_names: frozenset[str]) -> dict[str, Any]:
    """Invert ``_mark_entry_settings``: strip the synthetic ``__extend`` suffix off
    the entry's settings-patch fields so the entry re-parses against the real field
    names, preserving any genuine ``__extend`` marker still living *inside* the patch
    value (just as ``merge_with`` leaves it in ``settings_overrides``).
    """
    if not patch_field_names:
        return entry
    result: dict[str, Any] = {}
    for key, value in entry.items():
        if key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in patch_field_names:
            result[key[: -len(EXTEND_SUFFIX)]] = value
        else:
            result[key] = value
    return result


def _container_entry_classes(config: MngrConfig) -> dict[str, dict[Any, type[BaseModel]]]:
    """For each container-additive field, map its keys to the concrete model class
    of the entry stored there.

    The class is needed both to read its ``SettingsPatchField`` set (so a
    ``ClaudeAgentConfig`` entry's ``settings_overrides`` is marked ``__extend``) and
    so the merged entry can be re-parsed into the right subclass at the end. Read
    off the live config object rather than the field annotation, so a concrete
    subclass entry is recovered rather than the declared base type.
    """
    # ``dict(config)`` yields ``{field_name: value}`` for a pydantic model -- a
    # data-driven traversal by field name without ``getattr`` (the same shape
    # ``detect_settings_narrowing`` uses to walk fields generically).
    field_values = dict(config)
    classes: dict[str, dict[Any, type[BaseModel]]] = {}
    for field_name in _CONTAINER_DICT_FIELDS:
        container = field_values[field_name]
        classes[field_name] = {key: type(value) for key, value in container.items()}
    return classes


def _preprocess_container(
    container: dict[Any, Any],
    entry_classes: dict[Any, type[BaseModel]],
) -> dict[Any, Any]:
    """Pre-process one already-serialized container dict (``{key: entry_dict}``):
    mark each entry **key** ``<key>__extend`` and mark each entry's settings-patch
    fields ``__extend``.

    Two levels of ``__extend`` are needed to reproduce ``_merge_container_dict``:

    - The container field is ``__extend`` (caller) so overlay merges *per key*
      (key in one side carries through; key in both recurses) -- never an
      assign-replace of the whole dict.
    - Each entry key is *also* ``__extend`` so that, for a key present in both
      layers, overlay ``combine``\\s the two entry patches (recursing into the
      entry's fields) rather than letting the higher entry assign-replace the lower
      one wholesale. The recursion bottoms out at the entry's bare fields
      (assign-by-set, reproducing the entry ``merge_with``) and its
      ``__extend``-marked settings patch (accumulate). Without this second level a
      shared-key ``Default``-over-``Default`` would drop every base-only field of
      the entry (e.g. a base ``command`` when the override sets only ``cli_args``).
    """
    result: dict[Any, Any] = {}
    for key, entry in container.items():
        entry_class = entry_classes.get(key)
        patch_field_names = _settings_patch_field_names(entry_class) if entry_class is not None else frozenset()
        result[f"{key}{EXTEND_SUFFIX}"] = _mark_entry_settings(entry, patch_field_names)
    return result


def _to_operator_dict(
    values: dict[str, Any],
    entry_classes_by_field: dict[str, dict[Any, type[BaseModel]]],
    *,
    drop_none: bool,
) -> dict[str, Any]:
    """Pre-process a serialized ``MngrConfig`` layer dict into the operator language.

    - ``drop_none`` (the override side): every ``None``-valued top-level key is
      dropped. ``parse_config`` pads unset scalars (and ``retry`` / ``logging``) to
      ``None``, so this is the "treat ``None`` as unset" step that reproduces
      ``_assign_scalar`` / the ``if override.<field> is not None`` guards. The base
      side keeps its values (a base ``None`` scalar is the padding too, but it is
      what ``merge_with`` would carry as ``self.<field>`` and the final
      ``model_validate`` defaults it -- see ``finalize_like_loader``).
    - Each container-additive field (``agent_types`` ...) is renamed
      ``<field>__extend`` and its entries recursively settings-marked, so overlay
      deep-merges per key (reproducing ``_merge_container_dict``). An empty ``{}``
      container becomes an empty-payload ``__extend`` -- a no-op extend.
    - ``retry`` / ``logging`` stay bare nested dicts: overlay ``combine`` of the full
      base patch with the sparse override patch is exactly their set-fields-assign
      ``merge_with``.
    - Every other key stays bare (assign-by-set).
    """
    result: dict[str, Any] = {}
    for key, value in values.items():
        if drop_none and value is None:
            continue
        if key in _CONTAINER_DICT_FIELDS:
            entry_classes = entry_classes_by_field.get(key, {})
            result[f"{key}{EXTEND_SUFFIX}"] = _preprocess_container(value, entry_classes)
        else:
            result[key] = value
    return result


def _from_operator_dict(
    merged: dict[str, Any],
    entry_classes_by_field: dict[str, dict[Any, type[BaseModel]]],
) -> dict[str, Any]:
    """Invert ``_to_operator_dict``: strip the container ``__extend`` suffix and the
    per-entry settings-patch ``__extend`` suffix so the merged dict re-parses against
    the real ``MngrConfig`` / entry field names. Genuine ``__extend`` markers living
    *inside* a settings patch are left untouched.
    """
    result: dict[str, Any] = {}
    for key, value in merged.items():
        if key.endswith(EXTEND_SUFFIX) and key[: -len(EXTEND_SUFFIX)] in _CONTAINER_DICT_FIELDS:
            field_name = key[: -len(EXTEND_SUFFIX)]
            entry_classes = entry_classes_by_field.get(field_name, {})
            unmarked_container: dict[Any, Any] = {}
            for marked_entry_key, entry in value.items():
                # Each entry key was synthetically suffixed ``<key>__extend`` by
                # ``_preprocess_container``; strip it back to the real container key.
                entry_key = (
                    marked_entry_key[: -len(EXTEND_SUFFIX)]
                    if marked_entry_key.endswith(EXTEND_SUFFIX)
                    else marked_entry_key
                )
                entry_class = entry_classes.get(entry_key)
                patch_field_names = (
                    _settings_patch_field_names(entry_class) if entry_class is not None else frozenset()
                )
                unmarked_container[entry_key] = _unmark_entry_settings(entry, patch_field_names)
            result[field_name] = unmarked_container
        else:
            result[key] = value
    return result


def _reparse_container_entries(
    merged_dict: dict[str, Any],
    base_classes: dict[str, dict[Any, type[BaseModel]]],
    override_classes: dict[str, dict[Any, type[BaseModel]]],
) -> None:
    """Re-parse each container entry back into its concrete model class, in place.

    ``MngrConfig.model_validate`` cannot pick a subclass for a container entry (the
    declared value type is the base ``AgentTypeConfig`` / ``PluginConfig`` / ...), so
    a ``ClaudeAgentConfig`` entry would lose its subclass-only fields. Reproduce
    ``_merge_container_dict``'s class handling by re-parsing each entry dict into the
    class it came from: prefer the base entry's class (the lower layer, which
    ``merge_with`` keeps for a key present in both), falling back to the override
    entry's class for a key the override added.
    """
    for field_name in _CONTAINER_DICT_FIELDS:
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


def merge_mngr_config_via_overlay(base: MngrConfig, override: MngrConfig) -> MngrConfig:
    """Reproduce ``base.merge_with(override)`` for a whole ``MngrConfig`` via the
    overlay node algebra, never calling ``merge_with``.

    Pipeline:

    1. **Serialize.** ``base.model_dump()`` (full) and
       ``override.model_dump(exclude_unset=True)`` (sparse). Python mode -- same as
       ``merge_with`` -- so values round-trip; the re-parse re-coerces declared types.
    2. **Pre-process.** Drop ``None`` keys from the override (None-as-unset);
       ``__extend``-mark the container fields and recursively settings-mark their
       entries; leave ``retry`` / ``logging`` as bare nested dicts.
    3. **Merge.** ``lift`` both pre-processed dicts and ``combine`` override over
       base. ``combine`` (not ``merge_narrowing_allowed``) because this reproduces
       only the merged *value*; ``merge_with`` performs no narrowing.
    4. **Lower + re-parse.** ``lower`` the combined patch, strip the synthetic
       suffixes, re-parse each container entry into its concrete (sub)class, then
       ``MngrConfig.model_validate`` the whole dict.

    The returned config is the *pre-final-validation* merge equivalent: like
    ``merge_with``, an unset scalar that neither side wrote stays whatever the base
    dump carried for it (the padded ``None``), so callers should compare via
    ``finalize_like_loader`` (which applies the loader's final defaults to both
    sides) rather than directly. ``model_validate`` here is only what re-coerces the
    container/sub-model types; it tolerates the padded ``None`` scalars because their
    declared types admit ``None`` or because the base carried a concrete value.
    """
    base_classes = _container_entry_classes(base)
    override_classes = _container_entry_classes(override)
    # A class table spanning both layers, so an entry present only in the override
    # (whose key is absent from ``base``) still has its settings fields marked.
    merged_classes: dict[str, dict[Any, type[BaseModel]]] = {
        field_name: {**base_classes.get(field_name, {}), **override_classes.get(field_name, {})}
        for field_name in _CONTAINER_DICT_FIELDS
    }

    # ``serialize_as_any=True`` makes each value serialize with its *concrete*
    # type's serializer (duck-typed) rather than the field's declared type. Without
    # it, a container entry typed ``dict[..., AgentTypeConfig]`` would serialize a
    # ``ClaudeAgentConfig`` subclass entry through the base ``AgentTypeConfig``
    # serializer, silently dropping subclass-only fields (``auto_dismiss_dialogs``,
    # ``settings_overrides``). The production per-entry merge sidesteps this because
    # each entry's own ``model_dump`` runs the concrete serializer; this flag gives
    # the single top-level dump the same fidelity. It still honours ``exclude_unset``.
    base_full = base.model_dump(serialize_as_any=True)
    override_sparse = override.model_dump(exclude_unset=True, serialize_as_any=True)

    lower_patch = lift(_to_operator_dict(base_full, merged_classes, drop_none=False))
    higher_patch = lift(_to_operator_dict(override_sparse, merged_classes, drop_none=True))
    merged_patch = combine(lower_patch, higher_patch)

    merged_dict = _from_operator_dict(lower(merged_patch), merged_classes)
    _reparse_container_entries(merged_dict, base_classes, override_classes)
    return MngrConfig.model_validate(merged_dict)


def finalize_like_loader(config: MngrConfig) -> MngrConfig:
    """Apply the loader's *final* validation step to a (possibly padded) config,
    yielding the user-visible config with defaults filled in.

    Reproduces the tail of ``load_config`` exactly: build a ``config_dict`` by
    reading the field *values* off ``config`` (not a serialized dict), omitting the
    padded ``None`` scalars (so ``model_validate`` supplies their model defaults)
    while passing the container dicts and the explicitly-set sub-models through as
    live instances -- which is what ``load_config`` does
    (``config_dict["agent_types"] = config.agent_types``), so concrete container-
    entry subclasses keep their subclass-only fields rather than being re-coerced to
    the declared base type through a dump. This is the single faithful comparison
    point for ``base.merge_with(override)`` vs the overlay pipeline: both produce a
    pre-final-validation merge (unset scalars left ``None``), and both become the
    same user-visible config only after this default-applying step. It is applied to
    *both* sides of the equality, so it cannot mask a genuine value divergence -- a
    real difference in any set field survives ``model_validate`` untouched.
    """
    # ``dict(config)`` yields the field-name -> live-value map without ``getattr``.
    # Drop padded ``None`` scalars / unset ``retry`` / ``logging`` so
    # ``model_validate`` fills the model default (the loader's behavior for the
    # ``None``-guarded fields); pass everything else (containers, set sub-models,
    # concrete values) through as a live instance.
    config_dict: dict[str, Any] = {
        field_name: value for field_name, value in dict(config).items() if value is not None
    }
    return MngrConfig.model_validate(config_dict)


def parse_layer(raw: dict[str, Any]) -> MngrConfig:
    """Convenience for tests: parse a raw TOML-shaped dict into a ``MngrConfig`` the
    way the loader does (the padded ``parse_config`` construction), with no plugins
    disabled. Kept here so the test corpus is built through the *real* padded path
    -- the whole point of probing the top-level None-padding."""
    return parse_config(raw, frozenset())
