"""Render fields of an agent/host detail model for ``--fields`` and ``--format``.

Generic dotted-path field access (``host.resource.memory_gb``,
``host.snapshots[:3]``) and ``str.format``-style template expansion over a
pydantic model. Shared by ``mngr list`` and ``mngr state`` so both resolve field
names and render values identically.
"""

import re
import string
from collections.abc import Sequence
from enum import Enum
from typing import Any
from typing import Final

from pydantic import BaseModel

from imbue.imbue_common.pure import pure
from imbue.mngr.cli.output_helpers import render_format_template

# Aliases that let users reference fields by the same name in --fields/--format
# templates as they do in CEL filters and --sort. host.provider is the short form
# documented for CEL; the underlying attribute is host.provider_name.
FIELD_ALIASES: Final[dict[str, str]] = {
    "host.provider": "host.provider_name",
    # `project` is the documented short form for the project label, mirroring
    # the `--project` filter flag; the underlying data lives in labels.project.
    "project": "labels.project",
}

# Pattern to match a field part with optional bracket notation
# Matches: "fieldname", "fieldname[0]", "fieldname[-1]", "fieldname[:3]", "fieldname[1:3]", etc.
_BRACKET_PATTERN = re.compile(r"^([^\[]+)(?:\[([^\]]+)\])?$")


@pure
def resolve_field_alias(field: str) -> str:
    """Map a user-supplied field name to its canonical form for attribute/dict lookups."""
    return FIELD_ALIASES.get(field, field)


def _parse_slice_spec(spec: str) -> int | slice | None:
    """Parse a bracket slice specification like '0', '-1', ':3', '1:3', or '1:'.

    Returns an int for single index, slice object for ranges, or None if invalid.
    """
    spec = spec.strip()

    try:
        # Check if it's a slice (contains ':')
        if ":" in spec:
            parts = spec.split(":")
            if len(parts) == 2:
                start_str, stop_str = parts
                start = int(start_str) if start_str else None
                stop = int(stop_str) if stop_str else None
                return slice(start, stop)
            elif len(parts) == 3:
                start_str, stop_str, step_str = parts
                start = int(start_str) if start_str else None
                stop = int(stop_str) if stop_str else None
                step = int(step_str) if step_str else None
                return slice(start, stop, step)
            else:
                # Invalid slice format (too many colons)
                return None
        else:
            # Simple index
            return int(spec)
    except ValueError:
        # Could not parse integers in the spec
        return None


def _format_value_as_string(value: Any) -> str:
    """Convert a value to string representation for display."""
    if value is None:
        return ""
    elif isinstance(value, dict):
        if not value:
            return ""
        return ", ".join(f"{k}={v}" for k, v in value.items())
    elif isinstance(value, Enum):
        return str(value.value)
    elif hasattr(value, "name") and hasattr(value, "id"):
        # For objects like SnapshotInfo that have both name and id, prefer name
        return str(value.name)
    elif isinstance(value, (tuple, list)) and not isinstance(value, str):
        return ", ".join(_format_value_as_string(item) for item in value)
    elif isinstance(value, str):
        return value
    else:
        return str(value)


def get_field_value(model: BaseModel, field: str) -> str:
    """Extract a field value from a detail model and return it as a string.

    Supports nested fields like "host.name" and list slicing syntax like
    "host.snapshots[0]" or "host.snapshots[:3]". Unknown fields return "".
    """
    # Resolve aliases first so a user-supplied alias (e.g. host.provider) maps to the
    # canonical attribute name (host.provider_name) before walking the model.
    field = resolve_field_alias(field)
    # Handle nested fields (e.g., "host.name") with optional bracket notation
    # Also supports dict key access for plugin fields (e.g., "host.plugin.aws.iam_user")
    parts = field.split(".")
    value: Any = model

    try:
        for part in parts:
            # Parse the part for bracket notation
            match = _BRACKET_PATTERN.match(part)
            if not match:
                return ""

            field_name = match.group(1)
            # bracket_spec may be None if no brackets present in the part
            bracket_spec = match.group(2)

            # Get the field value: try object attribute first, then dict key
            if hasattr(value, field_name):
                value = getattr(value, field_name)
            elif isinstance(value, dict) and field_name in value:
                value = value[field_name]
            else:
                return ""

            # Apply bracket indexing/slicing if present
            if bracket_spec is not None:
                if not isinstance(value, (list, tuple, Sequence)) or isinstance(value, str):
                    return ""

                index_or_slice = _parse_slice_spec(bracket_spec)
                if index_or_slice is None:
                    return ""

                try:
                    value = value[index_or_slice]
                except (IndexError, ValueError):
                    # IndexError: out of bounds index
                    # ValueError: slice step cannot be zero
                    return ""

                # If the result is a list (from slicing), format each element
                if isinstance(value, (list, tuple)) and not isinstance(value, str):
                    return ", ".join(_format_value_as_string(item) for item in value)

        return _format_value_as_string(value)
    except (AttributeError, KeyError):
        return ""


@pure
def render_format_template_for_model(template: str, model: BaseModel) -> str:
    """Expand a str.format()-style template using field values from a detail model.

    Pre-resolves field names via :func:`get_field_value` (which supports nested
    attribute access and bracket notation), then delegates template expansion to
    the shared ``render_format_template`` helper.
    """
    field_values: dict[str, str] = {}
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None:
            field_values[field_name] = get_field_value(model, field_name)
    return render_format_template(template, field_values)
