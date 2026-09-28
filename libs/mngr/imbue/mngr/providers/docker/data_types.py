from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import DockerCpuCount
from imbue.mngr.primitives import DockerMemorySize


class ContainerSize(FrozenModel):
    """The CPU and memory caps a container's ``docker run`` arguments set, each None when uncapped."""

    cpus: float | None = Field(default=None, description="The `--cpus` cap, which docker allows to be fractional")
    memory: DockerMemorySize | None = Field(default=None, description="The `--memory` cap in docker's spelling")
    memory_swap: DockerMemorySize | None = Field(
        default=None, description="The `--memory-swap` cap in docker's spelling (memory plus swap)"
    )


class ContainerSizeRequest(FrozenModel):
    """The dimensions a caller wants to set on a container; a None dimension is left as it is."""

    cpus: DockerCpuCount | None = Field(default=None, description="Whole CPUs to cap the container at")
    memory: DockerMemorySize | None = Field(default=None, description="Memory to cap the container at")


class DockerDaemonTotals(FrozenModel):
    """The CPU count and total memory of the machine running the docker daemon, as ``docker info`` reports them."""

    cpu_count: int = Field(description="CPUs available to the daemon")
    memory_bytes: int = Field(description="Total memory of the daemon's machine in bytes")
