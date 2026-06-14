"""Generic Pydantic-model schema introspection for CLI ``--schema`` views.

Both ``mngr config list --schema`` (over ``MngrConfig``) and ``mngr list
--schema`` (over ``AgentDetails``/``HostDetails``) need to flatten a Pydantic
model's fields into dotted key paths with rendered types and descriptions. This
module holds the shared walk and the annotation renderer so the two commands
cannot drift in how they present a model's shape.
"""

import types
import typing
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from imbue.mngr.utils.pydantic_utils import unwrap_optional


def render_annotation(annotation: Any) -> str:
    """Render a type annotation as a short, user-facing string.

    Produces ``list[str]``, ``str | None``, ``Literal['agent']``,
    ``dict[str, Path]`` etc. -- preserving the generic parameters that tell a
    user what values a field takes, while stripping module qualifiers so the
    output reads like the source annotation rather than a fully-qualified repr
    (``imbue.mngr.primitives.HostState | None`` becomes ``HostState | None``).
    """
    if annotation is None or annotation is type(None):
        return "None"

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Literal:
        return "Literal[" + ", ".join(repr(arg) for arg in args) + "]"

    # Both typing.Union (Optional[X]) and the PEP 604 ``X | None`` form.
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(render_annotation(arg) for arg in args)

    if origin is not None:
        origin_name = origin.__name__ if isinstance(origin, type) else str(origin)
        if not args:
            return origin_name
        rendered_args = ", ".join("..." if arg is Ellipsis else render_annotation(arg) for arg in args)
        return f"{origin_name}[{rendered_args}]"

    if isinstance(annotation, type):
        return annotation.__name__
    return repr(annotation)


def _resolve_nested_model(
    annotation: Any,
    recurse_optional: bool,
    recurse_sequence: bool,
) -> type[BaseModel] | None:
    """Return the nested ``BaseModel`` to recurse into for ``annotation``, or None.

    A bare ``BaseModel`` subclass always qualifies. ``X | None`` qualifies only
    when ``recurse_optional`` is set (it is unwrapped to ``X`` via the shared
    ``unwrap_optional``, which -- per the repo's UP007 rule -- handles the
    PEP 604 form the codebase uses). ``list[Model]`` / ``tuple[Model, ...]``
    qualify only when ``recurse_sequence`` is set. Anything else (dicts, scalars,
    multi-arm unions) returns None so the field is emitted as a leaf.
    """
    if recurse_optional:
        annotation = unwrap_optional(annotation)

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation

    if recurse_sequence and typing.get_origin(annotation) in (list, tuple, set, frozenset):
        element_args = [arg for arg in typing.get_args(annotation) if arg is not Ellipsis]
        if len(element_args) == 1:
            return _resolve_nested_model(element_args[0], recurse_optional, recurse_sequence)

    return None


def walk_model_fields(
    model_class: type[BaseModel],
    prefix: tuple[str, ...] = (),
    recurse_optional: bool = False,
    recurse_sequence: bool = False,
    emit_container_rows: bool = False,
) -> Iterator[tuple[str, Any, str]]:
    """Yield ``(dotted_key, annotation, description)`` for each field of ``model_class``.

    Recurses into nested ``BaseModel`` fields so sub-models contribute their
    dotted paths (e.g. ``host.resource.cpu.count``). ``recurse_optional`` also
    descends through ``X | None`` wrappers; ``recurse_sequence`` descends
    through ``list[Model]`` element types. ``emit_container_rows`` additionally
    yields a row for each nested model itself (so ``host.resource`` appears
    alongside its leaves) -- callers that only care about settable leaves (e.g.
    config) leave it off.

    Container dicts (``labels``, ``host.tags``, ``plugin``) and leaf dicts both
    stop here: their inner key shape is user-extensible, not part of the schema.
    """
    for field_name, field_info in model_class.model_fields.items():
        annotation = field_info.annotation
        description = field_info.description or ""
        path = ".".join(prefix + (field_name,))
        nested = _resolve_nested_model(annotation, recurse_optional, recurse_sequence)
        if nested is not None:
            if emit_container_rows:
                yield path, annotation, description
            yield from walk_model_fields(
                nested,
                prefix=prefix + (field_name,),
                recurse_optional=recurse_optional,
                recurse_sequence=recurse_sequence,
                emit_container_rows=emit_container_rows,
            )
            continue
        yield path, annotation, description
