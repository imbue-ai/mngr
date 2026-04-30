from collections.abc import Sequence
from typing import Any

import celpy
from celpy.celparser import CELParseError
from celpy.evaluation import CELEvalError
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError


class TolerantMapType(celpy.celtypes.MapType):
    """A CEL MapType whose missing-key access returns None instead of raising.

    Used for schemaless fields (e.g. agent labels) where the absence of a key
    should evaluate to a clean False in equality checks rather than emit a
    per-agent warning at filter time.

    The canonical CEL idiom for this would be optional-type field selection
    (`labels.?key`, see cel-spec proposal 246), but cel-python does not yet
    implement optional types, so we hand-roll this targeted subclass instead.
    Drop this once cel-python supports `?`-prefixed field selection.

    Trade-off: cel-python's `has(map.key)` macro is implemented as "did the
    expression error?" (`celpy/evaluation.py::macro_has_eval`). Because
    TolerantMapType returns None instead of raising on missing keys, `has()`
    against a TolerantMapType always returns True. Use `map.key != null` or
    direct comparison (`map.key == "x"`) to test for presence on tolerant
    fields.
    """

    def __getitem__(self, key: Any) -> Any:
        try:
            return super().__getitem__(key)
        except KeyError:
            return None


class _TolerantDict(dict):
    """Marker subclass of dict signaling that the value should be converted to
    a TolerantMapType when the CEL context is built.

    Production sites use the public `tolerant_dict()` constructor; consumers
    only need to know that a `_TolerantDict` value in the raw context produces
    a `TolerantMapType` after `build_cel_context`.
    """


def tolerant_dict(value: dict[str, Any]) -> _TolerantDict:
    """Wrap a dict to mark it as schemaless for CEL filtering.

    The marker is honored anywhere in the value tree: marking a nested dict
    produces a TolerantMapType only at that level, with surrounding dicts
    converted to plain (strict) MapType.
    """
    return _TolerantDict(value)


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

    Container types (dict, list, tuple) are walked here so that `_TolerantDict`
    markers nested at any depth produce `TolerantMapType` (missing keys evaluate
    to None). Plain dicts produce strict `MapType`. Leaf types delegate to
    `celpy.json_to_cel` (which handles bool/int/float/str/datetime/None).
    """
    if isinstance(value, _TolerantDict):
        return TolerantMapType({celpy.json_to_cel(k): _convert_to_cel_value(v) for k, v in value.items()})
    if isinstance(value, dict):
        return celpy.celtypes.MapType({celpy.json_to_cel(k): _convert_to_cel_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return celpy.celtypes.ListType([_convert_to_cel_value(v) for v in value])
    return celpy.json_to_cel(value)


@pure
def build_cel_context(raw_context: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw dict to a CEL-compatible evaluation context."""
    return {k: _convert_to_cel_value(v) for k, v in raw_context.items()}


def apply_cel_filters_to_context(
    context: dict[str, Any],
    include_filters: Sequence[Any],
    exclude_filters: Sequence[Any],
    # Used in warning messages to identify what is being filtered
    error_context_description: str,
) -> bool:
    """Apply CEL filters to a context dictionary.

    Returns True if the context should be included (matches all include filters
    and doesn't match any exclude filters).

    Nested dictionaries in the context are automatically converted to CEL-compatible
    objects, enabling standard CEL dot notation (e.g., host.provider == "local").
    """
    # Convert nested dicts to CEL-compatible objects for dot notation support
    cel_context = build_cel_context(context)

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
