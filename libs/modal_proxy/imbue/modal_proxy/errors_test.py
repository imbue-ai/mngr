import pytest

from imbue.modal_proxy.errors import is_environment_not_found_error


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Canonical Modal SDK wording for a missing environment.
        pytest.param("Environment 'mngr-abc' not found", True, id="canonical_environment_not_found"),
        # The `[^']+` requires at least one char, so an empty name must NOT match.
        pytest.param("Environment '' not found", False, id="empty_environment_name"),
        # Without quotes around the name the message is not the environment form.
        pytest.param("Environment foo not found", False, id="no_quotes"),
        # A path-level not-found whose path merely contains "Environment" must
        # not be misclassified (mirrors the retry-predicate regression case).
        pytest.param("File '/Environment/x' not found", False, id="path_containing_environment_substring"),
        # The `^` anchor means trailing context is fine but leading text is not.
        pytest.param("Error: Environment 'mngr-abc' not found", False, id="leading_text_before_environment"),
        pytest.param("", False, id="empty_string"),
    ],
)
def test_is_environment_not_found_error_matches_only_environment_wording(message: str, expected: bool) -> None:
    assert is_environment_not_found_error(Exception(message)) is expected
