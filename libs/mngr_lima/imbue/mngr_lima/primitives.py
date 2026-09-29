import math
from typing import Self

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.primitives import ByteSize


class LimaCpuCount(PositiveInt):
    """Whole CPUs a lima VM boots with (``limactl start --cpus``). Must be > 0."""


class LimaMemoryGib(PositiveFloat):
    """RAM a lima VM boots with, in GiB, as ``limactl start --memory`` takes it. Must be finite and > 0."""

    def __new__(cls, value: float) -> Self:
        if not math.isfinite(value):
            raise InvalidPrimitiveValueError(f"{cls.__name__} must be a finite number of GiB, got {value}")
        return super().__new__(cls, value)


class LimaDiskSize(ByteSize):
    """A lima disk size (``100GiB``), as the ``disk`` config key and ``limactl disk create --size`` take it."""
