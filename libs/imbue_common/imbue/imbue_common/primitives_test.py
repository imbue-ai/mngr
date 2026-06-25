"""Tests for primitives."""

import pytest
from pydantic import BaseModel
from pydantic import ValidationError

from imbue.imbue_common.primitives import InvalidProbabilityError
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.imbue_common.primitives import Probability

# =============================================================================
# Tests for NonEmptyStr
# =============================================================================


def test_non_empty_str_valid() -> None:
    """NonEmptyStr should accept non-empty strings."""
    result = NonEmptyStr("hello")
    assert result == "hello"


def test_non_empty_str_strips_whitespace() -> None:
    """NonEmptyStr should strip whitespace."""
    result = NonEmptyStr("  hello  ")
    assert result == "hello"


def test_non_empty_str_raises_on_empty() -> None:
    """NonEmptyStr should raise ValueError on empty string."""
    with pytest.raises(ValueError, match="cannot be empty"):
        NonEmptyStr("")


def test_non_empty_str_raises_on_whitespace() -> None:
    """NonEmptyStr should raise ValueError on whitespace-only string."""
    with pytest.raises(ValueError, match="cannot be empty"):
        NonEmptyStr("   ")


# =============================================================================
# Tests for NonNegativeInt
# =============================================================================


def test_non_negative_int_zero() -> None:
    """NonNegativeInt should accept zero."""
    result = NonNegativeInt(0)
    assert result == 0


def test_non_negative_int_positive() -> None:
    """NonNegativeInt should accept positive integers."""
    result = NonNegativeInt(42)
    assert result == 42


def test_non_negative_int_raises_on_negative() -> None:
    """NonNegativeInt should raise ValueError on negative integers."""
    with pytest.raises(ValueError, match="must be >= 0"):
        NonNegativeInt(-1)


# =============================================================================
# Tests for PositiveInt
# =============================================================================


def test_positive_int_positive() -> None:
    """PositiveInt should accept positive integers."""
    result = PositiveInt(1)
    assert result == 1


def test_positive_int_raises_on_zero() -> None:
    """PositiveInt should raise ValueError on zero."""
    with pytest.raises(ValueError, match="must be > 0"):
        PositiveInt(0)


def test_positive_int_raises_on_negative() -> None:
    """PositiveInt should raise ValueError on negative integers."""
    with pytest.raises(ValueError, match="must be > 0"):
        PositiveInt(-1)


# =============================================================================
# Tests for NonNegativeFloat
# =============================================================================


def test_non_negative_float_zero() -> None:
    """NonNegativeFloat should accept zero."""
    result = NonNegativeFloat(0.0)
    assert result == 0.0


def test_non_negative_float_positive() -> None:
    """NonNegativeFloat should accept positive floats."""
    result = NonNegativeFloat(3.14)
    assert result == 3.14


def test_non_negative_float_raises_on_negative() -> None:
    """NonNegativeFloat should raise ValueError on negative floats."""
    with pytest.raises(ValueError, match="must be >= 0"):
        NonNegativeFloat(-0.01)


# =============================================================================
# Tests for PositiveFloat
# =============================================================================


def test_positive_float_positive() -> None:
    """PositiveFloat should accept positive floats."""
    result = PositiveFloat(0.001)
    assert result == 0.001


def test_positive_float_raises_on_zero() -> None:
    """PositiveFloat should raise ValueError on zero."""
    with pytest.raises(ValueError, match="must be > 0"):
        PositiveFloat(0.0)


def test_positive_float_raises_on_negative() -> None:
    """PositiveFloat should raise ValueError on negative floats."""
    with pytest.raises(ValueError, match="must be > 0"):
        PositiveFloat(-0.01)


# =============================================================================
# Tests for Probability
# =============================================================================


def test_probability_zero() -> None:
    """Probability should accept zero."""
    result = Probability(0.0)
    assert result == 0.0


def test_probability_one() -> None:
    """Probability should accept one."""
    result = Probability(1.0)
    assert result == 1.0


def test_probability_middle() -> None:
    """Probability should accept values between 0 and 1."""
    result = Probability(0.5)
    assert result == 0.5


def test_probability_raises_below_zero() -> None:
    """Probability should raise InvalidProbabilityError when below zero."""
    with pytest.raises(InvalidProbabilityError, match="must be between 0.0 and 1.0"):
        Probability(-0.1)


def test_probability_raises_above_one() -> None:
    """Probability should raise InvalidProbabilityError when above one."""
    with pytest.raises(InvalidProbabilityError, match="must be between 0.0 and 1.0"):
        Probability(1.1)


# =============================================================================
# Tests for Pydantic integration
# =============================================================================


def test_non_negative_float_pydantic_schema() -> None:
    """NonNegativeFloat should work in pydantic models via model_validate."""

    class TestModel(BaseModel):
        value: NonNegativeFloat

    model = TestModel.model_validate({"value": 3.14})
    assert model.value == 3.14
    assert isinstance(model.value, NonNegativeFloat)


def test_positive_float_pydantic_schema() -> None:
    """PositiveFloat should work in pydantic models via model_validate."""

    class TestModel(BaseModel):
        value: PositiveFloat

    model = TestModel.model_validate({"value": 0.5})
    assert model.value == 0.5
    assert isinstance(model.value, PositiveFloat)


def test_probability_pydantic_schema() -> None:
    """Probability should work in pydantic models via model_validate."""

    class TestModel(BaseModel):
        value: Probability

    model = TestModel.model_validate({"value": 0.75})
    assert model.value == 0.75
    assert isinstance(model.value, Probability)


def test_non_empty_str_pydantic_schema_accepts_and_strips() -> None:
    """NonEmptyStr should validate and strip through model_validate."""

    class TestModel(BaseModel):
        value: NonEmptyStr

    model = TestModel.model_validate({"value": "  hello  "})
    assert model.value == "hello"
    assert isinstance(model.value, NonEmptyStr)


def test_non_empty_str_pydantic_schema_rejects_blank() -> None:
    """NonEmptyStr's pydantic schema should reject empty/whitespace-only values."""

    class TestModel(BaseModel):
        value: NonEmptyStr

    with pytest.raises(ValidationError):
        TestModel.model_validate({"value": "   "})


def test_non_negative_int_pydantic_schema_rejects_negative() -> None:
    """NonNegativeInt's pydantic schema should reject negative values."""

    class TestModel(BaseModel):
        value: NonNegativeInt

    with pytest.raises(ValidationError):
        TestModel.model_validate({"value": -1})


def test_positive_int_pydantic_schema_rejects_zero() -> None:
    """PositiveInt's pydantic schema should reject zero."""

    class TestModel(BaseModel):
        value: PositiveInt

    with pytest.raises(ValidationError):
        TestModel.model_validate({"value": 0})


def test_non_negative_float_pydantic_schema_rejects_negative() -> None:
    """NonNegativeFloat's pydantic schema should reject negative values."""

    class TestModel(BaseModel):
        value: NonNegativeFloat

    with pytest.raises(ValidationError):
        TestModel.model_validate({"value": -0.01})


def test_positive_float_pydantic_schema_rejects_zero() -> None:
    """PositiveFloat's pydantic schema should reject zero."""

    class TestModel(BaseModel):
        value: PositiveFloat

    with pytest.raises(ValidationError):
        TestModel.model_validate({"value": 0.0})


def test_probability_pydantic_schema_rejects_out_of_range() -> None:
    """Probability's pydantic schema should reject values above 1.0."""

    class TestModel(BaseModel):
        value: Probability

    with pytest.raises(ValidationError):
        TestModel.model_validate({"value": 1.5})
