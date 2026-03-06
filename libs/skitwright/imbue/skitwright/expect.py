import re
from typing import overload

from imbue.skitwright.data_types import CommandResult


class ResultExpectation:
    """Fluent assertions on a CommandResult."""

    def __init__(self, result: CommandResult) -> None:
        self._result = result

    def _format_context(self) -> str:
        parts = [
            f"  Command: {self._result.command}",
            f"  Exit code: {self._result.exit_code}",
        ]
        if self._result.stdout.strip():
            parts.append(f"  Stdout:\n    {self._result.stdout.strip()}")
        if self._result.stderr.strip():
            parts.append(f"  Stderr:\n    {self._result.stderr.strip()}")
        return "\n".join(parts)

    def to_succeed(self) -> None:
        """Assert the command exited with code 0."""
        if not self._result.succeeded:
            raise AssertionError(
                f"Expected command to succeed but got exit code {self._result.exit_code}\n{self._format_context()}"
            )

    def to_fail(self) -> None:
        """Assert the command exited with a non-zero code."""
        if not self._result.failed:
            raise AssertionError(f"Expected command to fail but it succeeded\n{self._format_context()}")

    def to_have_exit_code(self, expected_code: int) -> None:
        """Assert a specific exit code."""
        if self._result.exit_code != expected_code:
            raise AssertionError(
                f"Expected exit code {expected_code} but got {self._result.exit_code}\n{self._format_context()}"
            )


class StringExpectation:
    """Fluent assertions on a string value."""

    def __init__(self, value: str, label: str = "string") -> None:
        self._value = value
        self._label = label

    def to_contain(self, substring: str) -> None:
        """Assert the string contains a substring."""
        if substring not in self._value:
            raise AssertionError(
                f"Expected {self._label} to contain {substring!r}\n  Actual value:\n    {self._value!r}"
            )

    def not_to_contain(self, substring: str) -> None:
        """Assert the string does not contain a substring."""
        if substring in self._value:
            raise AssertionError(
                f"Expected {self._label} not to contain {substring!r}\n  Actual value:\n    {self._value!r}"
            )

    def to_match(self, pattern: str) -> None:
        """Assert the string matches a regex pattern (search, not fullmatch)."""
        if not re.search(pattern, self._value):
            raise AssertionError(
                f"Expected {self._label} to match pattern {pattern!r}\n  Actual value:\n    {self._value!r}"
            )

    def not_to_match(self, pattern: str) -> None:
        """Assert the string does not match a regex pattern."""
        if re.search(pattern, self._value):
            raise AssertionError(
                f"Expected {self._label} not to match pattern {pattern!r}\n  Actual value:\n    {self._value!r}"
            )

    def to_equal(self, expected: str) -> None:
        """Assert exact string equality."""
        if self._value != expected:
            raise AssertionError(f"Expected {self._label} to equal {expected!r}\n  Actual value:\n    {self._value!r}")

    def to_be_empty(self) -> None:
        """Assert the string is empty."""
        if self._value:
            raise AssertionError(f"Expected {self._label} to be empty\n  Actual value:\n    {self._value!r}")


@overload
def expect(value: CommandResult) -> ResultExpectation: ...


@overload
def expect(value: str) -> StringExpectation: ...


def expect(value: CommandResult | str) -> ResultExpectation | StringExpectation:
    """Create a fluent expectation on a command result or string."""
    if isinstance(value, CommandResult):
        return ResultExpectation(value)
    if isinstance(value, str):
        return StringExpectation(value)
    raise TypeError(f"expect() does not support {type(value).__name__}")
