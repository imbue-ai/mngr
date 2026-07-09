from enum import StrEnum


class UpperCaseStrEnum(StrEnum):
    """A StrEnum that automatically converts enum member names to uppercase values."""

    @staticmethod
    def _generate_next_value_(
        name: str,
        start: int,
        count: int,
        last_values: list[str],
    ) -> str:
        return name.upper()


class LowerCaseStrEnum(StrEnum):
    """A StrEnum that automatically converts enum member names to lowercase values.

    For enums whose values are an externally visible, already-lowercase wire format
    (e.g. statuses in emitted JSON reports); prefer UpperCaseStrEnum for purely
    internal enums.
    """

    @staticmethod
    def _generate_next_value_(
        name: str,
        start: int,
        count: int,
        last_values: list[str],
    ) -> str:
        return name.lower()
