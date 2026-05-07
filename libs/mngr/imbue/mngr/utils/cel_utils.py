from collections.abc import Sequence
from typing import Any

import celpy
from celpy.celparser import CELParseError
from celpy.evaluation import CELEvalError
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError


class TolerantMapType(celpy.celtypes.MapType):
    """A CEL MapType whose missing-key access yields a CELEvalError value
    instead of raising, so that boolean expressions short-circuit cleanly.

    Used for schemaless fields (e.g. agent labels) where the absence of a key
    should evaluate to a clean False in equality checks rather than emit a
    per-agent warning at filter time.

    Returning a CELEvalError *value* (not raising) plays nicely with cel-python:
    its evaluator carries CELEvalError through arithmetic / comparison ops,
    so `labels.X == "Y"` short-circuits to BoolType(False) at the top level
    with no error escaping evaluate(); and `has(labels.X)` correctly returns
    False (cel-python's `has()` macro reports `not isinstance(_, CELEvalError)`,
    see `celpy/evaluation.py::macro_has_eval`).

    The canonical CEL idiom for this would be optional-type field selection
    (`labels.?key`, see cel-spec proposal 246), but cel-python does not yet
    implement optional types, so we hand-roll this targeted subclass instead.
    Drop this once cel-python supports `?`-prefixed field selection.

    Caveat: comparisons against `null` do not work as you might expect on a
    tolerant miss (`labels.X != null` evaluates to BoolType(True) even when X
    is absent, because the LHS is a CELEvalError, not null). Use `has(field)`
    as the canonical presence check.
    """

    def __getitem__(self, key: Any) -> Any:
        try:
            return super().__getitem__(key)
        except KeyError:
            return CELEvalError(f"no such member in mapping: {key!r}", KeyError, None)


@pure
def compile_cel_filters(
    include_filters: Sequence[str],
    exclude_filters: Sequence[str],
) -> tuple[list[Any], list[Any]]:
    """Compile CEL filter expressions into evaluable programs.

    Raises MngrError if any filter expression is invalid.
    """
    compiled_includes: list[Any] = []
    compiled_excludes: list[Any] = []

    env = celpy.Environment()

    for filter_expr in include_filters:
        try:
            ast = env.compile(filter_expr)
            prgm = env.program(ast)
            compiled_includes.append(prgm)
        except CELParseError as e:
            raise MngrError(f"Invalid include filter expression '{filter_expr}': {e}") from e

    for filter_expr in exclude_filters:
        try:
            ast = env.compile(filter_expr)
            prgm = env.program(ast)
            compiled_excludes.append(prgm)
        except CELParseError as e:
            raise MngrError(f"Invalid exclude filter expression '{filter_expr}': {e}") from e

    return compiled_includes, compiled_excludes


def _convert_to_cel_value(value: Any) -> Any:
    """Convert a raw Python value to a CEL-compatible value.

    Containers (dict, list, tuple) are walked here so the structure survives
    intact and produces strict `MapType` / `ListType`. Leaf types delegate to
    `celpy.json_to_cel` (which handles bool/int/float/str/datetime/None).

    Tolerance for schemaless fields is *not* baked in here — apply
    `replace_paths_with_tolerant_map` to a built context if you need it.
    """
    if isinstance(value, dict):
        return celpy.celtypes.MapType({celpy.json_to_cel(k): _convert_to_cel_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return celpy.celtypes.ListType([_convert_to_cel_value(v) for v in value])
    return celpy.json_to_cel(value)


@pure
def build_cel_context(raw_context: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw dict to a CEL-compatible evaluation context."""
    return {k: _convert_to_cel_value(v) for k, v in raw_context.items()}


def replace_paths_with_tolerant_map(
    cel_context: dict[str, Any],
    paths: Sequence[Sequence[str]],
) -> None:
    """For each `path` in `paths`, replace cel_context[path[0]]...[path[-1]]
    with a `TolerantMapType` wrapping the existing MapType at that location.

    Mutates `cel_context` in place. Each path navigates from the top of the
    CEL context; the target at the end of the path must already be a `MapType`
    (e.g. produced by `build_cel_context` from a raw dict). Use to opt specific
    schemaless fields into tolerant missing-key behavior without affecting
    sibling fields.

    Top-level keys in `cel_context` are plain Python strings; nested MapType
    keys are CEL StringType. Both look up correctly with plain `str` because
    StringType is a `str` subclass with consistent hash/eq.
    """
    for path in paths:
        *prefix, last = path
        parent: Any = cel_context
        for step in prefix:
            parent = parent[step]
        parent[last] = TolerantMapType(parent[last])


def apply_compiled_cel_filters(
    cel_context: dict[str, Any],
    include_filters: Sequence[Any],
    exclude_filters: Sequence[Any],
    # Used in warning messages to identify what is being filtered
    error_context_description: str,
) -> bool:
    """Apply CEL filters to an already-CEL-converted context.

    Returns True if the context should be included (matches all include filters
    and doesn't match any exclude filters). Use this when the caller wants to
    customize the CEL context (e.g. via `replace_paths_with_tolerant_map`)
    between conversion and filter evaluation; otherwise prefer
    `apply_cel_filters_to_context` which composes both steps.
    """
    for prgm in include_filters:
        try:
            result = prgm.evaluate(cel_context)
            if not result:
                return False
        except (CELEvalError, TypeError) as e:
            logger.warning("Error evaluating include filter on {}: {}", error_context_description, e)
            return False

    for prgm in exclude_filters:
        try:
            result = prgm.evaluate(cel_context)
            if result:
                return False
        except (CELEvalError, TypeError) as e:
            logger.warning("Error evaluating exclude filter on {}: {}", error_context_description, e)
            continue

    return True


def apply_cel_filters_to_context(
    context: dict[str, Any],
    include_filters: Sequence[Any],
    exclude_filters: Sequence[Any],
    # Used in warning messages to identify what is being filtered
    error_context_description: str,
) -> bool:
    """Apply CEL filters to a raw context dictionary.

    Returns True if the context should be included (matches all include filters
    and doesn't match any exclude filters). The raw context is converted to
    CEL-compatible types via `build_cel_context`, then evaluated.

    Callers that need tolerant missing-key behavior on specific paths should
    call `build_cel_context`, then `replace_paths_with_tolerant_map`, then
    `apply_compiled_cel_filters` directly.
    """
    cel_context = build_cel_context(context)
    return apply_compiled_cel_filters(
        cel_context=cel_context,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        error_context_description=error_context_description,
    )


@pure
def parse_cel_sort_spec(sort_spec: str) -> list[tuple[str, bool]]:
    """Parse a sort specification into (expression, is_descending) pairs.

    Format: "expr1 [asc|desc], expr2 [asc|desc], ..."
    Default direction is ascending.
    """
    keys: list[tuple[str, bool]] = []
    for part in sort_spec.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        # Check if the last whitespace-separated token is a direction keyword
        tokens = stripped.rsplit(maxsplit=1)
        if len(tokens) == 2 and tokens[1].lower() in ("asc", "desc"):
            expression = tokens[0].strip()
            is_descending = tokens[1].lower() == "desc"
        else:
            expression = stripped
            is_descending = False
        keys.append((expression, is_descending))
    return keys


@pure
def compile_cel_sort_keys(
    sort_spec: str,
) -> list[tuple[Any, bool]]:
    """Compile a sort specification into (program, is_descending) pairs.

    Raises MngrError if any sort expression is invalid CEL.
    """
    parsed = parse_cel_sort_spec(sort_spec)
    env = celpy.Environment()
    compiled: list[tuple[Any, bool]] = []
    for expression, is_descending in parsed:
        try:
            ast = env.compile(expression)
            prgm = env.program(ast)
            compiled.append((prgm, is_descending))
        except CELParseError as e:
            raise MngrError(f"Invalid sort expression '{expression}': {e}") from e
    return compiled


def evaluate_cel_sort_key(
    program: Any,
    cel_context: dict[str, Any],
) -> Any:
    """Evaluate a single CEL sort key against a pre-built CEL context.

    Returns the evaluated value, or None if evaluation fails.
    """
    try:
        return program.evaluate(cel_context)
    except CELEvalError as e:
        logger.trace("CEL sort key evaluation failed: {}", e)
        return None
