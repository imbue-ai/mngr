from typing import Any
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema


class InvalidPrimitiveValueError(ValueError):
    """Raised when a constrained primitive (non-empty string, non-negative/positive number) is given an invalid value."""


class NonEmptyStr(str):
    """A string that cannot be empty or whitespace-only."""

    def __new__(cls, value: str) -> Self:
        if not value or not value.strip():
            raise InvalidPrimitiveValueError(f"{cls.__name__} cannot be empty")
        return super().__new__(cls, value.strip())

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1),
            serialization=core_schema.to_string_ser_schema(),
        )


class NonNegativeInt(int):
    """An integer that must be >= 0."""

    def __new__(cls, value: int) -> Self:
        if value < 0:
            raise InvalidPrimitiveValueError(f"{cls.__name__} must be >= 0, got {value}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.int_schema(ge=0),
        )


class PositiveInt(int):
    """An integer that must be > 0."""

    def __new__(cls, value: int) -> Self:
        if value <= 0:
            raise InvalidPrimitiveValueError(f"{cls.__name__} must be > 0, got {value}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.int_schema(gt=0),
        )


class NonNegativeFloat(float):
    """A float that must be >= 0."""

    def __new__(cls, value: float) -> Self:
        if value < 0:
            raise InvalidPrimitiveValueError(f"{cls.__name__} must be >= 0, got {value}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.float_schema(ge=0),
        )


class PositiveFloat(float):
    """A float that must be > 0."""

    def __new__(cls, value: float) -> Self:
        if value <= 0:
            raise InvalidPrimitiveValueError(f"{cls.__name__} must be > 0, got {value}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.float_schema(gt=0),
        )


class InvalidProbabilityError(ValueError):
    """Raised when a probability value is out of range."""


class Probability(float):
    """The chance of an event. Must be between 0.0 and 1.0 (inclusive)."""

    def __new__(cls, value: float) -> Self:
        if not 0.0 <= value <= 1.0:
            raise InvalidProbabilityError(f"Probability must be between 0.0 and 1.0, got {value}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.float_schema(ge=0, le=1),
        )
