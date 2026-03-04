from collections.abc import Sequence
from typing import Any

import celpy
from celpy.celparser import CELParseError
from celpy.evaluation import CELEvalError
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mng.errors import MngError


@pure
def compile_cel_filters(
    include_filters: Sequence[str],
    exclude_filters: Sequence[str],
) -> tuple[list[Any], list[Any]]:
    """Compile CEL filter expressions into evaluable programs.

    Raises MngError if any filter expression is invalid.
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
            raise MngError(f"Invalid include filter expression '{filter_expr}': {e}") from e

    for filter_expr in exclude_filters:
        try:
            ast = env.compile(filter_expr)
            prgm = env.program(ast)
            compiled_excludes.append(prgm)
        except CELParseError as e:
            raise MngError(f"Invalid exclude filter expression '{filter_expr}': {e}") from e

    return compiled_includes, compiled_excludes


def _convert_to_cel_value(value: Any) -> Any:
    """Convert a Python value to a CEL-compatible value.

    All values are converted using celpy.json_to_cel() so that CEL string methods
    (contains, startsWith, endsWith) work correctly on string values, and nested
    dicts support dot notation access.
    """
    return celpy.json_to_cel(value)


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
    cel_context = {k: _convert_to_cel_value(v) for k, v in context.items()}

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
