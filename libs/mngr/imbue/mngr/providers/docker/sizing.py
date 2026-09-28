import math
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.primitives import DockerCpuCount
from imbue.mngr.primitives import DockerMemorySize
from imbue.mngr.primitives import InvalidDockerMemorySizeError
from imbue.mngr.providers.docker.data_types import ContainerSize
from imbue.mngr.providers.docker.data_types import ContainerSizeRequest
from imbue.mngr.providers.docker.data_types import DockerDaemonTotals
from imbue.mngr.providers.start_arg_flags import flag_value_at
from imbue.mngr.providers.start_arg_flags import strip_flags

_CPUS_FLAG: Final[str] = "--cpus"
_MEMORY_FLAG: Final[str] = "--memory"
_MEMORY_SHORT_FLAG: Final[str] = "-m"
_MEMORY_SWAP_FLAG: Final[str] = "--memory-swap"

# Every spelling docker accepts for the CPU cap, the memory cap, and the swap cap.
_CPUS_FLAGS: Final[tuple[str, ...]] = (_CPUS_FLAG,)
_MEMORY_FLAGS: Final[tuple[str, ...]] = (_MEMORY_FLAG, _MEMORY_SHORT_FLAG)
_MEMORY_SWAP_FLAGS: Final[tuple[str, ...]] = (_MEMORY_SWAP_FLAG,)

# Docker's spelling for "no swap cap at all", as opposed to a swap cap of some size.
_UNLIMITED_SWAP_VALUE: Final[str] = "-1"


@pure
def parse_container_size(start_args: Sequence[str]) -> ContainerSize:
    """The caps a ``docker run`` argument list sets; the last spelling of each flag wins, like docker's CLI.

    A ``0`` cap is docker's "no limit" and reads as uncapped; ``--memory-swap=-1``
    is its "no swap cap" and is kept apart from a swap cap of some size.
    """
    cpus: float | None = None
    memory: DockerMemorySize | None = None
    memory_swap: DockerMemorySize | None = None
    is_swap_unlimited = False
    idx = 0
    while idx < len(start_args):
        cpus_value, cpus_span = flag_value_at(start_args, idx, _CPUS_FLAGS)
        if cpus_span:
            if cpus_value is not None:
                cpus = _parse_cpus_or_warn(cpus_value)
            idx += cpus_span
            continue
        memory_value, memory_span = flag_value_at(start_args, idx, _MEMORY_FLAGS)
        if memory_span:
            if memory_value is not None:
                memory = _parse_memory_or_warn(memory_value, _MEMORY_FLAG)
            idx += memory_span
            continue
        swap_value, swap_span = flag_value_at(start_args, idx, _MEMORY_SWAP_FLAGS)
        if swap_span:
            if swap_value is not None:
                is_swap_unlimited = swap_value == _UNLIMITED_SWAP_VALUE
                memory_swap = None if is_swap_unlimited else _parse_memory_or_warn(swap_value, _MEMORY_SWAP_FLAG)
            idx += swap_span
            continue
        idx += 1
    return ContainerSize(cpus=cpus, memory=memory, memory_swap=memory_swap, is_swap_unlimited=is_swap_unlimited)


@pure
def _parse_cpus_or_warn(value: str) -> float | None:
    """A recorded ``--cpus`` value, or None when it is docker's ``0`` (no limit) or not a positive finite number."""
    try:
        cpus = float(value)
    except ValueError:
        logger.warning("Ignored an unparseable recorded --cpus value: {!r}", value)
        return None
    if cpus < 0 or not math.isfinite(cpus):
        logger.warning("Ignored a recorded --cpus value docker does not accept: {!r}", value)
        return None
    return cpus if cpus > 0 else None


@pure
def _parse_memory_or_warn(value: str, flag: str) -> DockerMemorySize | None:
    """A recorded memory value, or None when it is docker's ``0`` (no limit) or not a size."""
    try:
        size = DockerMemorySize(value)
    except InvalidDockerMemorySizeError:
        logger.warning("Ignored an unparseable recorded {} value: {!r}", flag, value)
        return None
    return size if size.size_bytes > 0 else None


@pure
def strip_size_start_args(
    start_args: Sequence[str],
    is_cpus_stripped: bool,
    is_memory_stripped: bool,
) -> tuple[str, ...]:
    """``start_args`` without the CPU cap and/or without the memory and swap caps, in every spelling."""
    stripped_flags: list[str] = []
    if is_cpus_stripped:
        stripped_flags.extend(_CPUS_FLAGS)
    if is_memory_stripped:
        stripped_flags.extend(_MEMORY_FLAGS)
        stripped_flags.extend(_MEMORY_SWAP_FLAGS)
    return strip_flags(start_args, stripped_flags)


@pure
def render_size_start_args(request: ContainerSizeRequest) -> tuple[str, ...]:
    """The ``docker run`` flags for the dimensions ``request`` sets.

    Swap is capped at the memory cap, so a container at its limit is shed by the
    OOM killer instead of swapping the machine to a halt.
    """
    rendered: list[str] = []
    if request.cpus is not None:
        rendered.append(f"{_CPUS_FLAG}={request.cpus}")
    if request.memory is not None:
        rendered.append(f"{_MEMORY_FLAG}={request.memory}")
        rendered.append(f"{_MEMORY_SWAP_FLAG}={request.memory}")
    return tuple(rendered)


@pure
def apply_size_request(start_args: Sequence[str], request: ContainerSizeRequest) -> tuple[str, ...]:
    """``start_args`` with each dimension ``request`` sets replaced by its new value; unset dimensions are untouched."""
    stripped = strip_size_start_args(
        start_args,
        is_cpus_stripped=request.cpus is not None,
        is_memory_stripped=request.memory is not None,
    )
    return stripped + render_size_start_args(request)


@pure
def docker_update_args(size: ContainerSize) -> tuple[str, ...]:
    """The ``docker update`` flags that apply ``size`` to an existing container; empty when nothing is capped.

    Docker refuses a memory cap above the container's current swap cap unless
    the swap cap is updated with it, so the two are always sent together: the
    recorded swap setting when there is one (a cap, or ``-1`` for unlimited
    swap), else swap capped at the memory cap. That default is mngr's no-swap
    policy, and it brings a recorded bare ``--memory`` (a user-supplied start
    arg) under it from the first re-apply.
    """
    rendered: list[str] = []
    if size.cpus is not None:
        rendered.extend([_CPUS_FLAG, _format_cpus(size.cpus)])
    if size.memory is not None:
        if size.is_swap_unlimited:
            memory_swap = _UNLIMITED_SWAP_VALUE
        elif size.memory_swap is not None:
            memory_swap = str(size.memory_swap)
        else:
            memory_swap = str(size.memory)
        rendered.extend([_MEMORY_FLAG, str(size.memory), _MEMORY_SWAP_FLAG, memory_swap])
    return tuple(rendered)


@pure
def _format_cpus(cpus: float) -> str:
    """A CPU cap as docker prints it: a whole number stays whole (``2``, not ``2.0``)."""
    return str(int(cpus)) if float(cpus).is_integer() else str(cpus)


@pure
def clamp_cpus_to_daemon(cpus: DockerCpuCount, daemon_totals: DockerDaemonTotals) -> DockerCpuCount:
    """The largest whole CPU count the daemon accepts up to ``cpus`` (docker refuses a cap above its CPU count)."""
    return DockerCpuCount(min(cpus, max(1, daemon_totals.cpu_count)))


@pure
def host_resources_for_container(
    size: ContainerSize,
    # Consulted only for a dimension with no recorded cap, so a fully capped host needs no daemon read.
    read_daemon_totals: Callable[[], DockerDaemonTotals],
) -> HostResources:
    """What a container can use: each recorded cap, or the daemon machine's total where it is uncapped.

    A fractional CPU cap is reported rounded up, the way gVisor sizes the
    container's own ``nproc``. Disk is None: the host volume is a subpath of a
    shared named volume with no quota of its own.
    """
    cpu_count = math.ceil(size.cpus) if size.cpus is not None else read_daemon_totals().cpu_count
    memory_gb = size.memory.size_gb if size.memory is not None else read_daemon_totals().memory_bytes / 1024**3
    return HostResources(
        cpu=CpuResources(count=max(1, cpu_count), frequency_ghz=None),
        memory_gb=memory_gb,
        disk_gb=None,
        gpu=None,
    )
