import pytest
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel


class _Example(FrozenModel):
    name: str


def test_frozen_model_enforces_its_config_at_runtime() -> None:
    """``FrozenModel`` must actually reject mutation, not merely declare that it will.

    Every other ``is_frozen`` test asserts the declaration alone, through
    ``assert_model_is_frozen``, because the type checker reports a direct assignment
    to a frozen field before the test can reach the runtime failure. This is the one
    place that enforcement is exercised, so pydantic ceasing to honour
    ``frozen=True`` fails here rather than nowhere.
    """
    example = _Example(name="before")
    with pytest.raises(ValidationError):
        example.name = "after"  # ty: ignore[invalid-assignment]
