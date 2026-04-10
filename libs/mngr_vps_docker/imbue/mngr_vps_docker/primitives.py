from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr


class VpsInstanceId(NonEmptyStr):
    """Unique identifier for a VPS instance as assigned by the provider."""


class VpsSnapshotId(NonEmptyStr):
    """Unique identifier for a VPS-level snapshot."""


class VpsInstanceStatus(UpperCaseStrEnum):
    """Status of a VPS instance as reported by the provider API."""

    PENDING = auto()
    ACTIVE = auto()
    HALTED = auto()
    DESTROYING = auto()
    UNKNOWN = auto()
