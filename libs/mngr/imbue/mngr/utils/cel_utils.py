import copy
from collections.abc import Sequence
from typing import Any

import celpy
from celpy.celparser import CELParseError
from celpy.celtypes import MapType
from celpy.evaluation import CELEvalError
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError

# Marker substring embedded in the CELEvalError message produced by
# TolerantMapType on a missing-key access. apply_compiled_cel_filters checks
# for this substring to distinguish a tolerant miss (suppress warning) from
# any other CELEvalError flowing through the filter loop (log warning).
_TOLERANT_MISS_MARKER = "__tolerant_key_miss__"


class TolerantPathError(MngrError, TypeError):
    """Raised by `with_tolerant_paths` when a path is misconfigured.

    Indicates a programming error in the caller's `paths` argument: a path
    segment is missing from its parent dict, or an ancestor/target is not a
    dict-like (e.g. a MapType). Subclasses both `MngrError` (the single
    user-facing parent for all mngr errors, so an uncaught instance renders
    as a clean ``Error: ...`` at the CLI) and `TypeError` (so existing
    callers that catch TypeError still see it); the named subclass exists so
    call sites can distinguish a tolerant-paths setup error from any other
    TypeError flowing through the same code path.
    """


class TolerantMapType(MapType):
    """A CEL MapType whose missing-key access yields a marked CELEvalError
    value, so that `apply_compiled_cel_filters` can suppress the per-agent
    warning that would otherwise fire on missing schemaless-field keys.

    Used for schemaless fields (e.g. agent labels) where the absence of a key
    should evaluate to a clean False in equality checks rather than emit a
    per-agent warning at filter time.

    The CELEvalError value carries an internal marker substring
    (`_TOLERANT_MISS_MARKER`) that the filter loop matches on. cel-python's
    behavior on a CELEvalError flowing through `==` differs between versions
    (0.4.0 folds it silently to BoolType(False); 0.5.0 propagates and raises
    at the top level), so the marker-and-suppress approach is the
    version-portable way to keep the warning quiet on a tolerant miss
    without affecting unrelated CELEvalError flows.

    `has(labels.X)` correctly returns False on a missing tolerant key:
    cel-python's `has()` macro reports `not isinstance(_, CELEvalError)`
    (see `celpy/evaluation.py::macro_has_eval`).

    The canonical CEL idiom for this would be optional-type field selection
    (`labels.?key`, see cel-spec proposal 246), but cel-python does not yet
    implement optional types, so we hand-roll this targeted subclass instead.
    Drop this once cel-python supports `?`-prefixed field selection.

    Caveat: comparisons against `null` do not work as a presence check on a
    tolerant miss. On 0.5.0 the LHS is a CELEvalError that propagates through
    `==` / `!=` and raises at the top of evaluate(); the filter loop then
    sees the marker and suppresses the warning, so the filter result is
    False (an `--include 'labels.X != null'` rejects the agent rather than
    matching it). Use `has(field)` as the canonical presence check.
    """

    def __getitem__(self, key: Any) -> Any:
        try:
            return super().__getitem__(key)
        except KeyError:
            return CELEvalError(
                f"{_TOLERANT_MISS_MARKER} no such member in mapping: {key!r}",
                KeyError,
                (),
            )


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
    """Convert a raw Python value to a strict CEL-compatible value.

    Delegates to `celpy.json_to_cel`, which handles leaf types
    (bool/int/float/str/datetime/None) and recurses through containers
    (dict -> MapType, list/tuple -> ListType).

    Tolerance for schemaless fields is not applied here -- pass the built
    context through `with_tolerant_paths` if you need it.
    """
    return celpy.json_to_cel(value)


@pure
def build_cel_context(raw_context: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw dict to a CEL-compatible evaluation context."""
    return {k: _convert_to_cel_value(v) for k, v in raw_context.items()}


def with_tolerant_paths(
    cel_context: dict[str, Any],
    paths: Sequence[Sequence[str]],
) -> dict[str, Any]:
    """Return a deep copy of `cel_context` where each `path` target is wrapped
    in `TolerantMapType`. The input is unchanged.

    Each `path` navigates from the top of the CEL context; every segment of
    the path must already exist, and the target at the end of the path must
    already be a `MapType` (e.g. produced by `build_cel_context` from a raw
    dict). Use to opt specific schemaless fields into tolerant missing-key
    behavior without affecting siblings.

    Raises `TolerantPathError` (a `TypeError` subclass) on precondition
    violations (a path's segment is missing or any segment's value is not
    a dict/MapType), so misconfigured paths surface immediately rather
    than silently no-op.

    Top-level keys in `cel_context` are plain Python strings; nested MapType
    keys are CEL StringType. Both look up correctly with plain `str` because
    StringType is a `str` subclass with consistent hash/eq.
    """
    new_context = copy.deepcopy(cel_context)
    for path in paths:
        if not path:
            raise TolerantPathError("with_tolerant_paths: each path must have at least one segment; got an empty path")
        *prefix, last = path
        parent: Any = new_context
        for step in prefix:
            if not isinstance(parent, dict):
                raise TolerantPathError(
                    f"with_tolerant_paths: cannot descend through non-dict node "
                    f"of type {type(parent).__name__} at path step {step!r}; check "
                    f"that every path targets a MapType in the CEL context"
                )
            if step not in parent:
                raise TolerantPathError(
                    f"with_tolerant_paths: path segment {step!r} is not present "
                    f"in node with keys {sorted(str(k) for k in parent)!r}; "
                    f"check `paths` for a typo or unsupported path"
                )
            parent = parent[step]
        if not isinstance(parent, dict):
            raise TolerantPathError(
                f"with_tolerant_paths: cannot wrap non-dict node of type "
                f"{type(parent).__name__} at path target {last!r}; check that "
                f"every path targets a MapType in the CEL context"
            )
        if last not in parent:
            raise TolerantPathError(
                f"with_tolerant_paths: path target {last!r} is not present in "
                f"node with keys {sorted(str(k) for k in parent)!r}; check "
                f"`paths` for a typo or unsupported path"
            )
        target = parent[last]
        if not isinstance(target, dict):
            raise TolerantPathError(
                f"with_tolerant_paths: target at path-final segment {last!r} is "
                f"a {type(target).__name__}, not a MapType; tolerance only "
                f"applies to dict-like CEL values"
            )
        parent[last] = TolerantMapType(target)
    return new_context


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
    customize the CEL context (e.g. via `with_tolerant_paths`) between
    conversion and filter evaluation; otherwise prefer
    `apply_cel_filters_to_context` which composes both steps.
    """
    for prgm in include_filters:
        try:
            result = prgm.evaluate(cel_context)
            if not result:
                return False
        except (CELEvalError, TypeError) as e:
            if not _is_tolerant_miss(e):
                logger.warning("Error evaluating include filter on {}: {}", error_context_description, e)
            return False

    for prgm in exclude_filters:
        try:
            result = prgm.evaluate(cel_context)
            if result:
                return False
        except (CELEvalError, TypeError) as e:
            if not _is_tolerant_miss(e):
                logger.warning("Error evaluating exclude filter on {}: {}", error_context_description, e)
            continue

    return True


def _is_tolerant_miss(exc: BaseException) -> bool:
    """Return True if `exc` is the CELEvalError raised by a TolerantMapType miss.

    Detected via the marker substring `_TOLERANT_MISS_MARKER` embedded in the
    error message. Used by the filter loop to silence the per-agent warning
    that would otherwise fire on missing keys under schemaless fields.
    """
    return isinstance(exc, CELEvalError) and bool(exc.args) and _TOLERANT_MISS_MARKER in str(exc.args[0])


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
    call `build_cel_context`, then `with_tolerant_paths`, then
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
