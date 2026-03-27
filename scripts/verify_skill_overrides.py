"""Mngr-specific overrides for vet-generated code issue categories.

These overrides are applied AFTER generating from vet, so the vet base is always
the starting point. They add new categories and extend existing ones with
mngr-specific guidance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OverrideAction(Enum):
    ADD_CATEGORY = "add_category"
    APPEND_GUIDE = "append_guide"
    APPEND_EXAMPLES = "append_examples"
    APPEND_EXCEPTIONS = "append_exceptions"


@dataclass(frozen=True)
class Override:
    issue_code: str
    action: OverrideAction
    content: str


# New categories to add, keyed by issue_code -> (guide_text, insert_after).
# insert_after is the issue_code after which the new category should be inserted.
NEW_CATEGORIES: dict[str, tuple[str, str]] = {
    "commit_contents": (
        """\
The diff should not include excessive changes, or changes unrelated to the user's request. In particular, avoid:

1. Checking in binaries, compiled files, dependencies, or build artifacts
2. Accidental deletion of files or folders
3. Unrequested changes to test time limits, config vars, minimum required test coverage, ratchet test values, and any other settings that are supposed to constrain the codebase. It is *very* important to flag these issues as a MAJOR issue!""",
        "commit_message_mismatch",
    ),
    "test_quality": (
        """\
Any tests added in the diff should be of high quality individually, and should collectively create a high-quality test suite. This means:
- Avoid pointless and trivial tests
- Avoid creating lots of highly repetitive tests (parameterize the test or check all cases in a single test instead of making a separate test for each case, when appropriate)
- Ensure that common test code is factored out into fixtures
- Ensure that existing fixtures are used (when applicable)
- Ensure that tests are robust (ex: wait for conditions to be met rather than using hard-coded sleep statements, use appropriate timeouts, avoid flakiness)
- Ensure that the overall test suite for the changes is comprehensive and covers the new functionality well, but without creating more tests than necessary
- Ensure that functionality is tested with unit tests whenever possible, only creating a small number of slower integration tests when necessary
- Ensure that multiple integration tests for similar functionality are serving unique purposes and are not overly repetitive or duplicative
- Ensure that the tests are as fast and simple as possible
- Ensure that individual tests are clearly named and easy to understand""",
        "test_coverage",
    ),
}

# Overrides that extend existing vet categories.
CATEGORY_EXTENSIONS: list[Override] = [
    Override(
        issue_code="incomplete_integration_with_existing_code",
        action=OverrideAction.APPEND_GUIDE,
        content="""\
- Tests should be given the correct decorators (ex: @pytest.mark.acceptance for tests that require network access/credentials and @pytest.mark.release for end-to-end tests that are not "core", eg, test rarer cases)
- Tests should be placed in the correctly named file (ex: *_test.py for unit tests, test_*.py for integration/acceptance/release tests)""",
    ),
    Override(
        issue_code="refactoring_needed",
        action=OverrideAction.APPEND_GUIDE,
        content="""\
- This also includes structures that are unsafe (ex: returning a type that has an error state rather than raising an exception).
- Using primitive types (strings, integers, etc) to represent domain-level data--actual data types should be preferred instead, even if they simply inherit from the built-in types, as it makes the code more readable.
- Using an if/elif/.../else construct where you could use a match statement instead (eg, to dispatch on an enum value)""",
    ),
    Override(
        issue_code="refactoring_needed",
        action=OverrideAction.APPEND_EXAMPLES,
        content="""\
- A function that returns a value that can be either a valid result or an error state (e.g. None, False, -1) instead of raising an exception for the error case.
- A class that has a "name" attribute that is just a string, instead of having a proper Name class.
- An if/elif/.../else construct that dispatches on the value of an enum, instead of using a match statement.""",
    ),
    Override(
        issue_code="repetitive_or_duplicate_code",
        action=OverrideAction.APPEND_EXAMPLES,
        content="""\
- This is particularly common in tests, where multiple test cases may duplicate setup or validation logic that could be shared (e.g. as a fixture). It is important to flag such cases as a MAJOR issue!""",
    ),
    Override(
        issue_code="test_coverage",
        action=OverrideAction.APPEND_EXCEPTIONS,
        content="""\
- Changes *to the test code itself* (ex: to a conftest.py, testing_utils.py, test_*.py or *_test.py file) do not require test coverage (they will be executed when the tests run).""",
    ),
    Override(
        issue_code="fails_silently",
        action=OverrideAction.APPEND_GUIDE,
        content="""\
- Any "except" clause that does *not* log the error (at least at "trace" level) and/or report it to an error tracking system. Real error conditions should be logged at least at warning level, and anything that violates a program invariant should generally be raised.
- Any except clause must either log the error (if it is handling the error), or re-raise the error (if it is not handling the error).""",
    ),
    Override(
        issue_code="runtime_error_risk",
        action=OverrideAction.APPEND_GUIDE,
        content="""\
- Catch clauses that are too broad and could hide runtime errors: Almost all try/except blocks should only span a single line, and should generally catch a single class of errors.""",
    ),
    Override(
        issue_code="incorrect_algorithm",
        action=OverrideAction.APPEND_GUIDE,
        content="""\
- Any reimplementation of complex algorithms that should be imported from standard libraries or well-known packages (ex: use max flow from networkx instead of reimplementing it)""",
    ),
]
