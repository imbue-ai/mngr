from collections.abc import Sequence
from typing import Final

from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory

EXIT_CODE_SUCCESS: Final[int] = 0
EXIT_CODE_ERROR: Final[int] = 1
EXIT_CODE_TIMEOUT: Final[int] = 2
# Cleanup failure codes (see specs/cleanup-error-aggregation.md).
EXIT_CODE_PROCESSES_REMAIN: Final[int] = 3
EXIT_CODE_LOCAL_STATE_REMAINS: Final[int] = 4
EXIT_CODE_HOST_RESOURCE_REMAINS: Final[int] = 5
EXIT_CODE_PROVIDER_INACCESSIBLE: Final[int] = 6

# Maps each cleanup failure category to its process exit code.
_EXIT_CODE_BY_CATEGORY: Final[dict[CleanupFailureCategory, int]] = {
    CleanupFailureCategory.TIMEOUT: EXIT_CODE_TIMEOUT,
    CleanupFailureCategory.PROCESSES_REMAIN: EXIT_CODE_PROCESSES_REMAIN,
    CleanupFailureCategory.LOCAL_STATE_REMAINS: EXIT_CODE_LOCAL_STATE_REMAINS,
    CleanupFailureCategory.HOST_RESOURCE_REMAINS: EXIT_CODE_HOST_RESOURCE_REMAINS,
    CleanupFailureCategory.PROVIDER_INACCESSIBLE: EXIT_CODE_PROVIDER_INACCESSIBLE,
    CleanupFailureCategory.OTHER: EXIT_CODE_ERROR,
}

# Cleanup failure categories ordered from most to least severe. When a cleanup run hits
# several causes, the process exits with the code of the most severe one (the structured
# output still enumerates them all). Rationale: leaked paid infrastructure is worst; live
# processes next; inert local state and timeout (unknown) below that; "couldn't attempt"
# and uncategorized last.
_CATEGORY_SEVERITY_ORDER: Final[tuple[CleanupFailureCategory, ...]] = (
    CleanupFailureCategory.HOST_RESOURCE_REMAINS,
    CleanupFailureCategory.PROCESSES_REMAIN,
    CleanupFailureCategory.LOCAL_STATE_REMAINS,
    CleanupFailureCategory.TIMEOUT,
    CleanupFailureCategory.PROVIDER_INACCESSIBLE,
    CleanupFailureCategory.OTHER,
)


def exit_code_for_failures(failures: Sequence[CleanupFailure]) -> int:
    """Return the process exit code for a set of cleanup failures.

    ``EXIT_CODE_SUCCESS`` when there are no real failures; otherwise the code of the
    most severe category present (per ``_CATEGORY_SEVERITY_ORDER``).
    """
    if not failures:
        return EXIT_CODE_SUCCESS
    present = {failure.category for failure in failures}
    for category in _CATEGORY_SEVERITY_ORDER:
        if category in present:
            return _EXIT_CODE_BY_CATEGORY[category]
    # Every category is in the severity order, so this is unreachable; fall back defensively.
    return EXIT_CODE_ERROR
