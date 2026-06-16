from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr


class VpsInstanceId(NonEmptyStr):
    """Unique identifier for a VPS instance as assigned by the provider."""


class IsolationMode(UpperCaseStrEnum):
    """How the agent is isolated on its VPS -- the realization axis of a provider.

    Selects the ``HostRealizer`` the provider uses to place an agent on a booted
    VPS. ``CONTAINER`` (the default) runs the agent inside a Docker container;
    ``NONE`` runs it directly on the VPS OS (no container). Leaves room for a
    future sandboxed level (e.g. gVisor) that folds today's ``docker_runtime``
    knob into this enum.
    """

    CONTAINER = auto()
    NONE = auto()


class VpsInstanceStatus(UpperCaseStrEnum):
    """Status of a VPS instance as reported by the provider API."""

    PENDING = auto()
    ACTIVE = auto()
    HALTED = auto()
    DESTROYING = auto()
    UNKNOWN = auto()
