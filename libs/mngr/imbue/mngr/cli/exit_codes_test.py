import pytest

from imbue.mngr.cli.exit_codes import EXIT_CODE_ERROR
from imbue.mngr.cli.exit_codes import EXIT_CODE_HOST_RESOURCE_REMAINS
from imbue.mngr.cli.exit_codes import EXIT_CODE_LOCAL_STATE_REMAINS
from imbue.mngr.cli.exit_codes import EXIT_CODE_PROCESSES_REMAIN
from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.mngr.cli.exit_codes import EXIT_CODE_SUCCESS
from imbue.mngr.cli.exit_codes import EXIT_CODE_TIMEOUT
from imbue.mngr.cli.exit_codes import _CATEGORY_SEVERITY_ORDER
from imbue.mngr.cli.exit_codes import _EXIT_CODE_BY_CATEGORY
from imbue.mngr.cli.exit_codes import exit_code_for_failures
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory


def _failure(category: CleanupFailureCategory) -> CleanupFailure:
    """A CleanupFailure with the given category (other fields are irrelevant here)."""
    return CleanupFailure(category=category, message=f"{category.value} failure")


def test_no_failures_is_success() -> None:
    assert exit_code_for_failures([]) == EXIT_CODE_SUCCESS


@pytest.mark.parametrize(
    ("category", "expected_code"),
    [
        (CleanupFailureCategory.TIMEOUT, EXIT_CODE_TIMEOUT),
        (CleanupFailureCategory.PROCESSES_REMAIN, EXIT_CODE_PROCESSES_REMAIN),
        (CleanupFailureCategory.LOCAL_STATE_REMAINS, EXIT_CODE_LOCAL_STATE_REMAINS),
        (CleanupFailureCategory.HOST_RESOURCE_REMAINS, EXIT_CODE_HOST_RESOURCE_REMAINS),
        (CleanupFailureCategory.PROVIDER_INACCESSIBLE, EXIT_CODE_PROVIDER_INACCESSIBLE),
        (CleanupFailureCategory.OTHER, EXIT_CODE_ERROR),
    ],
)
def test_single_category_maps_to_its_code(category: CleanupFailureCategory, expected_code: int) -> None:
    assert exit_code_for_failures([_failure(category)]) == expected_code


@pytest.mark.parametrize(
    ("categories", "expected_code"),
    [
        # Leaked paid infrastructure outranks "couldn't even attempt".
        (
            (CleanupFailureCategory.PROVIDER_INACCESSIBLE, CleanupFailureCategory.HOST_RESOURCE_REMAINS),
            EXIT_CODE_HOST_RESOURCE_REMAINS,
        ),
        # Live processes outrank inert local state.
        (
            (CleanupFailureCategory.LOCAL_STATE_REMAINS, CleanupFailureCategory.PROCESSES_REMAIN),
            EXIT_CODE_PROCESSES_REMAIN,
        ),
        # Timeout (unknown state) outranks the uncategorized OTHER bucket.
        (
            (CleanupFailureCategory.OTHER, CleanupFailureCategory.TIMEOUT),
            EXIT_CODE_TIMEOUT,
        ),
        # The full set still resolves to the single most-severe cause.
        (tuple(CleanupFailureCategory), EXIT_CODE_HOST_RESOURCE_REMAINS),
    ],
)
def test_mixed_failures_use_most_severe_category(
    categories: tuple[CleanupFailureCategory, ...], expected_code: int
) -> None:
    failures = [_failure(category) for category in categories]
    assert exit_code_for_failures(failures) == expected_code
    # Order of the input must not change the result.
    assert exit_code_for_failures(list(reversed(failures))) == expected_code


def test_every_category_is_ranked_and_mapped() -> None:
    """Guards the documented-unreachable fallback: a new category must be added to both tables."""
    all_categories = set(CleanupFailureCategory)
    assert set(_CATEGORY_SEVERITY_ORDER) == all_categories
    assert set(_EXIT_CODE_BY_CATEGORY) == all_categories
