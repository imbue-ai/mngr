"""Declarative field markers for the config-merge pipeline and the generic helpers
that read them off pydantic field metadata.

A leaf module: it imports nothing from ``data_types`` or ``overlay_merge`` (both of
which import *from* it), so the marker types and their collectors can be shared by the
merge pipeline without a cycle.
"""

from pydantic import BaseModel


class SettingsPatchField:
    """Marker attached (via ``Annotated[dict[str, Any], SettingsPatchField()]``) to
    a dict field whose cross-layer merge **accumulates** as a settings *patch*
    rather than assigning by default.

    ``merge_with`` (config-scope merge) and ``_apply_custom_overrides_to_parent_config``
    (agent-type ``parent_type`` inheritance) read this marker off the field's
    ``model_fields[name].metadata``. A marked field is merged via the overlay node
    algebra's recursive, marker-preserving combine (the field is treated as
    ``__extend``) so a lower/parent layer's contribution is never dropped wholesale --
    even for non-overlapping keys, which an assign would clobber. Every other field
    stays assign-by-default.

    The field carrying this marker (``ClaudeAgentConfig.settings_overrides``) lives
    on a plugin subclass; the base ``merge_with`` reads the marker generically, so
    core never has to know the field's name. Because such a field accumulates
    (combine, never assign), a higher layer that merely adds keys is a superset and
    cannot narrow; only a bare assign that drops a non-empty aggregate *inside* the
    patch is surfaced as a narrowing (by the overlay merge, at any depth).
    """


class RegistryField:
    """Marker attached (via ``Annotated[dict[K, V], RegistryField()]``) to a top-level
    dict-of-models *registry* field whose cross-scope merge is **per key** (instead of
    assign-by-default).

    The five ``MngrConfig`` registries (``agent_types``, ``providers``, ``plugins``,
    ``commands``, ``create_templates``) carry it. The overlay merge reads the marker off
    the field metadata (``get_registry_field_names``) and merges each marked dict per key:
    a key present in one scope carries through, a key present in both has its entries
    merged field-by-field (the entry's own merge). A same-named dict nested *inside* a
    sub-model (e.g. a plugin config's own ``commands`` dict) carries no marker and so is an
    ordinary assign-by-default aggregate, narrowing-checked as a leaf. Parallel to
    ``SettingsPatchField`` but a distinct concept: a settings patch *accumulates* keys; a
    registry merges its entries per key.
    """


def get_field_names_with_marker(model_class: type[BaseModel], marker_type: type) -> frozenset[str]:
    """Return the field names of ``model_class`` whose metadata carries a ``marker_type``
    marker instance.

    Read off the pydantic field metadata so the overlay merge pipeline marks exactly the
    annotated fields, without hard-coding any field name. A class with no such fields
    yields an empty set.
    """
    return frozenset(
        name
        for name, field in model_class.model_fields.items()
        if any(isinstance(item, marker_type) for item in field.metadata)
    )


def get_settings_patch_field_names(model_class: type[BaseModel]) -> frozenset[str]:
    """Return the ``SettingsPatchField``-marked field names of a model class."""
    return get_field_names_with_marker(model_class, SettingsPatchField)


def get_registry_field_names(model_class: type[BaseModel]) -> frozenset[str]:
    """Return the ``RegistryField``-marked field names of a model class."""
    return get_field_names_with_marker(model_class, RegistryField)
