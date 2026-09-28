import pytest

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.mngr_lima.primitives import LimaMemoryGib


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), float("-inf")])
def test_lima_memory_gib_rejects_what_limactl_could_not_boot_with(value: float) -> None:
    with pytest.raises(InvalidPrimitiveValueError):
        LimaMemoryGib(value)


def test_lima_memory_gib_keeps_a_positive_finite_value() -> None:
    assert LimaMemoryGib(1.5) == 1.5
