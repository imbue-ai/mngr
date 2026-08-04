from collections.abc import Mapping
from collections.abc import Sequence
from typing import Annotated
from typing import Any
from typing import Final
from typing import Literal

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.cel_utils import apply_compiled_cel_filters
from imbue.mngr.utils.cel_utils import build_cel_context
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.cel_utils import with_tolerant_paths
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import entries_shown_on_board
from imbue.mngr_kanpan.errors import KanpanError

# Braced name standing for the number of agents on the board.
_TOTAL_TEMPLATE_NAME: Final[str] = "total"
# Every agent matches, so `{total}` needs no counting rule of its own.
_TOTAL_EXPRESSION: Final[str] = "true"

# Board-entry keys holding one map per column: which columns exist depends on the
# configured data sources, and a column's payload shape depends on its kind.
_ENTRY_SCHEMALESS_ROOTS: Final[tuple[str, ...]] = ("fields", "cells")

# CEL string literals may contain braces, so the scanner tracks quoting to find
# the brace that actually closes an expression.
_QUOTE_CHARACTERS: Final[tuple[str, ...]] = ("'", '"')


class KanpanHeaderStatusError(KanpanError, ValueError):
    """Raised when `header_status` is misconfigured."""

    ...


class _LiteralSegment(FrozenModel):
    """Template text outside any braces, copied to the header verbatim."""

    kind: Literal["literal"] = "literal"
    text: str = Field(description="The literal text")


class _CountSegment(FrozenModel):
    """A braced CEL expression, rendered as the number of agents it holds for."""

    kind: Literal["count"] = "count"
    expression: str = Field(description="The expression as written, e.g. 'state == \"RUNNING\"'")
    program: Any = Field(description="The compiled CEL program")


_Segment = Annotated[_LiteralSegment | _CountSegment, Field(discriminator="kind")]


class HeaderStatus(FrozenModel):
    """A compiled `header_status` template."""

    segments: tuple[_Segment, ...] = Field(description="Literal text and counts, in template order")


@pure
def _compile_count(expression: str) -> Any:
    """Compile one count expression into an evaluable CEL program."""
    try:
        compiled_includes, _unused_excludes = compile_cel_filters([expression], [])
    except MngrError as e:
        raise KanpanHeaderStatusError(f"header_status count {expression!r} is not valid CEL: {e}") from e
    return compiled_includes[0]


@pure
def _scan_expression(template: str, start: int) -> tuple[str, int]:
    """Read the expression opening at `start`, returning it and the index past its `}`.

    Braces nest, and braces inside a CEL string literal do not count, so a map
    literal or a `}` in a compared string cannot end the expression early.
    """
    depth = 1
    quote: str | None = None
    index = start
    while index < len(template):
        character = template[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = None
        elif character in _QUOTE_CHARACTERS:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return template[start:index].strip(), index + 1
        else:
            # Any other character is part of the expression and carries no scan state.
            pass
        index += 1
    raise KanpanHeaderStatusError(
        f"header_status {template!r} has a '{{' at position {start - 1} that is never closed"
    )


@pure
def _parse_segments(template: str) -> tuple[_Segment, ...]:
    """Split `template` into its literal text and the CEL expressions braced within it.

    `{{` and `}}` stand for literal braces.
    """
    segments: list[_Segment] = []
    literal: list[str] = []
    index = 0
    while index < len(template):
        character = template[index]
        if character in ("{", "}") and template[index : index + 2] == character * 2:
            literal.append(character)
            index += 2
            continue
        if character == "}":
            raise KanpanHeaderStatusError(
                f"header_status {template!r} has a '}}' at position {index} that closes nothing; "
                f"write '}}}}' for a literal brace"
            )
        if character == "{":
            expression, index = _scan_expression(template, index + 1)
            if not expression:
                raise KanpanHeaderStatusError(f"header_status {template!r} has an empty '{{}}'")
            if literal:
                segments.append(_LiteralSegment(text="".join(literal)))
                literal = []
            segments.append(
                _CountSegment(expression=expression, program=_compile_count(_count_expression(expression)))
            )
            continue
        literal.append(character)
        index += 1
    if literal:
        segments.append(_LiteralSegment(text="".join(literal)))
    return tuple(segments)


@pure
def _count_expression(written: str) -> str:
    """The CEL to compile for a braced expression."""
    return _TOTAL_EXPRESSION if written == _TOTAL_TEMPLATE_NAME else written


@pure
def _check_config_type(template: str | None) -> None:
    """Check that `header_status` holds the type it is declared with.

    Plugin configs are built with `model_construct`, which bypasses pydantic
    validation, so it arrives as whatever the user's TOML held.
    """
    if template is not None and not isinstance(template, str):
        raise KanpanHeaderStatusError(f"header_status must be a string, got {type(template).__name__}")


@pure
def compile_header_status(template: str | None) -> HeaderStatus | None:
    """Compile the header status template, or None when unset."""
    _check_config_type(template)
    if template is None:
        return None
    return HeaderStatus(segments=_parse_segments(template))


@pure
def _entry_tolerant_paths(cel_context: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Paths in `cel_context` under which a missing key evaluates False rather than raising.

    Each schemaless root and, beneath it, every column the entry carries, so that a
    count naming a member the column's payload does not have -- `fields.pr.state` on
    an agent whose PR fetch failed -- is not counted rather than warning per agent.
    A root comes before its columns so the wrapped root is the one descended into.
    """
    return tuple(
        path
        for root in _ENTRY_SCHEMALESS_ROOTS
        for path in ((root,), *((root, str(column)) for column in cel_context[root]))
    )


@pure
def _entry_cel_context(entry: AgentBoardEntry) -> dict[str, Any]:
    """CEL evaluation context for one board entry -- the shape `--format json` emits for it."""
    cel_context = build_cel_context(entry.model_dump(mode="json"))
    return with_tolerant_paths(cel_context, _entry_tolerant_paths(cel_context))


def _is_counted(segment: _CountSegment, cel_context: dict[str, Any], agent_name: AgentName) -> bool:
    """Whether one count expression holds for an entry's CEL context."""
    return apply_compiled_cel_filters(
        cel_context=cel_context,
        include_filters=[segment.program],
        exclude_filters=[],
        error_context_description=f"count {segment.expression!r} on agent {agent_name}",
    )


def render_header_status(
    status: HeaderStatus | None,
    snapshot: BoardSnapshot | None,
    section_order: Sequence[BoardSection],
) -> str:
    """Header status text for `snapshot`, empty when unconfigured or before the first fetch.

    Counts run over the entries the board is showing -- those in `section_order`, after
    any active filter -- so they agree with the board and with `--format json`.
    """
    if status is None or snapshot is None:
        return ""
    entries = entries_shown_on_board(snapshot, section_order)
    count_segments = tuple(segment for segment in status.segments if isinstance(segment, _CountSegment))
    tallies = [0] * len(count_segments)
    for entry in entries:
        cel_context = _entry_cel_context(entry)
        for index, segment in enumerate(count_segments):
            if _is_counted(segment, cel_context, entry.name):
                tallies[index] += 1

    parts: list[str] = []
    tally_index = 0
    for segment in status.segments:
        if isinstance(segment, _LiteralSegment):
            parts.append(segment.text)
        else:
            parts.append(str(tallies[tally_index]))
            tally_index += 1
    return "".join(parts)
