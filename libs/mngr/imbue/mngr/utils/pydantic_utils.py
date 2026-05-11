import types
import typing
from typing import Any

from imbue.imbue_common.pure import pure


@pure
def unwrap_optional(annotation: Any) -> Any:
    """If `annotation` is `X | None` (a.k.a. `Optional[X]`), return `X`; otherwise return as-is.

    Handles both the `typing.Union[X, None]` form and the PEP 604 `X | None`
    form (`types.UnionType`). A Union with more than two non-None args is
    returned unchanged.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation
