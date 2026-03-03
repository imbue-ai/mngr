import types
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin

from pydantic import BaseModel

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.pure import pure


class InvalidFieldPathError(ValueError):
    """Raised when a dotted field path does not match the model's field hierarchy."""

    def __init__(self, full_path: str, invalid_segment: str, valid_fields: frozenset[str]) -> None:
        self.full_path = full_path
        self.invalid_segment = invalid_segment
        self.valid_fields = valid_fields
        super().__init__(
            f"Unknown field path: '{full_path}'. "
            f"'{invalid_segment}' is not a valid field. "
            f"Valid fields at this level: {', '.join(sorted(valid_fields))}"
        )


@pure
def resolve_model_type(annotation: Any) -> type[BaseModel] | None:
    """Extract a BaseModel subclass from a type annotation, unwrapping Optional/list/tuple.

    Returns None for primitive types and dict (dynamic keys).
    Raises SwitchError for annotation forms that are not handled.
    """
    origin = get_origin(annotation)

    # Handle X | None (Optional types)
    if origin is types.UnionType or origin is Union:
        args = get_args(annotation)
        if len(args) != 2 or type(None) not in args:
            raise SwitchError(f"Cannot resolve non-optional union type: {annotation}")
        inner = args[0] if args[1] is type(None) else args[1]
        return resolve_model_type(inner)

    # Handle list[X]
    elif origin is list:
        args = get_args(annotation)
        if not args or len(args) != 1:
            raise SwitchError(f"Expected list[X], got: {annotation}")
        return resolve_model_type(args[0])

    # Handle tuple[X, ...] (homogeneous variable-length tuples)
    elif origin is tuple:
        args = get_args(annotation)
        if len(args) != 2 or args[1] is not Ellipsis:
            raise SwitchError(f"Expected tuple[X, ...], got: {annotation}")
        return resolve_model_type(args[0])

    # Handle dict[K, V] -- dynamic keys, stop resolution
    elif origin is dict:
        return None

    # No generic origin -- either a direct model class or a primitive type
    elif origin is None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        return None

    else:
        raise SwitchError(f"Unhandled annotation origin: {origin} (from annotation {annotation})")


@pure
def validate_field_path(model: type[BaseModel], field_path: str) -> None:
    """Validate that a dotted field path refers to known fields in the model hierarchy.

    Raises InvalidFieldPathError if any segment of the path is not recognized.
    """
    parts = field_path.split(".")
    current_model = model

    for part in parts:
        field_name = part.split("[")[0]

        if field_name not in current_model.model_fields:
            raise InvalidFieldPathError(
                full_path=field_path,
                invalid_segment=field_name,
                valid_fields=frozenset(current_model.model_fields.keys()),
            )

        # Resolve the type of this field to check deeper levels
        field_info = current_model.model_fields[field_name]
        next_model = resolve_model_type(field_info.annotation)
        if next_model is None:
            # Reached a primitive, dict, or non-model type -- can't validate further
            return
        current_model = next_model
