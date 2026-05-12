import types
import typing
from typing import Any

from imbue.imbue_common.pure import pure


@pure
def unwrap_optional(annotation: Any) -> Any:
    """If `annotation` is `X | None`, return `X`; otherwise return as-is.

    Only handles the PEP 604 `X | None` form (a `types.UnionType` origin),
    which is the only form this codebase uses (the UP007 ruff rule enforces
    PEP 604 over `typing.Optional` / `typing.Union`). A union with more than
    one non-None arm is returned unchanged.
    """
    if typing.get_origin(annotation) is types.UnionType:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation
