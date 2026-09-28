from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.primitives import ByteSize


class LimaCpuCount(PositiveInt):
    """Whole CPUs a lima VM boots with (``limactl start --cpus``). Must be > 0."""


class LimaMemoryGib(PositiveFloat):
    """RAM a lima VM boots with, in GiB, as ``limactl start --memory`` takes it. Must be > 0."""


class LimaDiskSize(ByteSize):
    """A lima disk size (``100GiB``), as the ``disk`` config key and ``limactl disk create --size`` take it."""
