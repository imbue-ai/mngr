import math
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.primitives import ByteSize
from imbue.mngr.primitives import InvalidByteSizeError
from imbue.mngr_lima.data_types import LimaSizeRequest
from imbue.mngr_lima.data_types import LimaVmSize
from imbue.mngr_lima.data_types import ResolvedLimaVmSize
from imbue.mngr_lima.primitives import LimaDiskSize

_CPUS_FLAG: Final[str] = "--cpus"
_MEMORY_FLAG: Final[str] = "--memory"
_DISK_FLAG: Final[str] = "--disk"

# What lima gives a VM whose config and start arguments leave a dimension unset.
LIMA_DEFAULT_CPUS: Final[int] = 4
LIMA_DEFAULT_MEMORY_GIB: Final[float] = 4.0
LIMA_DEFAULT_BOOT_DISK_GIB: Final[float] = 100.0

_BYTES_PER_GIB: Final[int] = 1024**3


@pure
def _flag_value_at(start_args: Sequence[str], idx: int, flag: str) -> tuple[str | None, int]:
    """The value ``flag`` carries at ``idx`` and how many tokens it spans (0 when not a match).

    Handles the ``--flag=value`` and ``--flag value`` forms limactl accepts.
    """
    token = start_args[idx]
    if token == flag:
        if idx + 1 < len(start_args):
            return start_args[idx + 1], 2
        return None, 1
    if token.startswith(f"{flag}="):
        return token[len(flag) + 1 :], 1
    return None, 0


@pure
def _parse_positive_number_or_warn(value: str, flag: str) -> float | None:
    """A recorded size flag's value, or None when it is not a positive finite number."""
    try:
        number = float(value)
    except ValueError:
        logger.warning("Ignored an unparseable recorded {} value: {!r}", flag, value)
        return None
    if number <= 0 or not math.isfinite(number):
        logger.warning("Ignored a recorded {} value lima does not accept: {!r}", flag, value)
        return None
    return number


@pure
def parse_vm_size_start_args(start_args: Sequence[str]) -> LimaVmSize:
    """The sizes a ``limactl start`` argument list sets; the last spelling of each flag wins, like limactl."""
    cpus: int | None = None
    memory_gib: float | None = None
    boot_disk_gib: float | None = None
    idx = 0
    while idx < len(start_args):
        cpus_value, cpus_span = _flag_value_at(start_args, idx, _CPUS_FLAG)
        if cpus_span:
            if cpus_value is not None:
                parsed_cpus = _parse_positive_number_or_warn(cpus_value, _CPUS_FLAG)
                cpus = int(parsed_cpus) if parsed_cpus is not None else None
            idx += cpus_span
            continue
        memory_value, memory_span = _flag_value_at(start_args, idx, _MEMORY_FLAG)
        if memory_span:
            if memory_value is not None:
                memory_gib = _parse_positive_number_or_warn(memory_value, _MEMORY_FLAG)
            idx += memory_span
            continue
        disk_value, disk_span = _flag_value_at(start_args, idx, _DISK_FLAG)
        if disk_span:
            if disk_value is not None:
                boot_disk_gib = _parse_positive_number_or_warn(disk_value, _DISK_FLAG)
            idx += disk_span
            continue
        idx += 1
    return LimaVmSize(cpus=cpus, memory_gib=memory_gib, boot_disk_gib=boot_disk_gib)


@pure
def strip_size_start_args(
    start_args: Sequence[str],
    is_cpus_stripped: bool,
    is_memory_stripped: bool,
) -> tuple[str, ...]:
    """``start_args`` without the CPU count and/or the memory size, in both spellings."""
    stripped_flags: list[str] = []
    if is_cpus_stripped:
        stripped_flags.append(_CPUS_FLAG)
    if is_memory_stripped:
        stripped_flags.append(_MEMORY_FLAG)
    kept: list[str] = []
    idx = 0
    while idx < len(start_args):
        span = 0
        for flag in stripped_flags:
            _value, span = _flag_value_at(start_args, idx, flag)
            if span:
                break
        if span:
            idx += span
            continue
        kept.append(start_args[idx])
        idx += 1
    return tuple(kept)


@pure
def format_gib(value: float) -> str:
    """A GiB value as limactl prints it: a whole number stays whole (``8``, not ``8.0``)."""
    return f"{value:g}"


@pure
def apply_size_request_to_start_args(start_args: Sequence[str], request: LimaSizeRequest) -> tuple[str, ...]:
    """``start_args`` with each VM dimension ``request`` sets replaced by its new value.

    The data disk is not a start argument (it is a separate lima disk), so only
    the CPU count and memory are rewritten here.
    """
    stripped = strip_size_start_args(
        start_args,
        is_cpus_stripped=request.cpus is not None,
        is_memory_stripped=request.memory_gib is not None,
    )
    rendered: list[str] = []
    if request.cpus is not None:
        rendered.append(f"{_CPUS_FLAG}={request.cpus}")
    if request.memory_gib is not None:
        rendered.append(f"{_MEMORY_FLAG}={format_gib(request.memory_gib)}")
    return stripped + tuple(rendered)


@pure
def _gib_from_lima_config_value(value: object, key: str) -> float | None:
    """A lima config size value (``"4GiB"`` or a bare number of GiB) in GiB, or None when absent or unparseable."""
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning("Ignored a lima config {} value that is not a size: {!r}", key, value)
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return ByteSize(value).size_gb
        except InvalidByteSizeError:
            logger.warning("Ignored an unparseable lima config {} value: {!r}", key, value)
            return None
    logger.warning("Ignored a lima config {} value that is not a size: {!r}", key, value)
    return None


@pure
def vm_size_from_lima_config(lima_config: Mapping[str, Any]) -> LimaVmSize:
    """The sizes a lima instance config sets (``cpus``, ``memory``, ``disk``); a missing or unparseable key is None."""
    config_cpus = lima_config.get("cpus")
    cpus = config_cpus if isinstance(config_cpus, int) and not isinstance(config_cpus, bool) and config_cpus > 0 else None
    return LimaVmSize(
        cpus=cpus,
        memory_gib=_gib_from_lima_config_value(lima_config.get("memory"), "memory"),
        boot_disk_gib=_gib_from_lima_config_value(lima_config.get("disk"), "disk"),
    )


@pure
def vm_size_from_resources(resources: HostResources, is_disk_gb_the_boot_disk: bool) -> LimaVmSize:
    """The VM size a host's recorded resources describe.

    ``disk_gb`` is the boot disk only on the exposed layout; on the btrfs layout
    it is the data disk and the boot disk is not recorded at all.
    """
    return LimaVmSize(
        cpus=resources.cpu.count,
        memory_gib=resources.memory_gb,
        boot_disk_gib=resources.disk_gb if is_disk_gb_the_boot_disk else None,
    )


@pure
def resolve_vm_size(start_args: Sequence[str], fallback: LimaVmSize) -> ResolvedLimaVmSize:
    """The size a VM boots with: its start arguments, else ``fallback``, else lima's own defaults."""
    from_start_args = parse_vm_size_start_args(start_args)
    if from_start_args.cpus is not None:
        cpus = from_start_args.cpus
    elif fallback.cpus is not None:
        cpus = fallback.cpus
    else:
        cpus = LIMA_DEFAULT_CPUS
    if from_start_args.memory_gib is not None:
        memory_gib = from_start_args.memory_gib
    elif fallback.memory_gib is not None:
        memory_gib = fallback.memory_gib
    else:
        memory_gib = LIMA_DEFAULT_MEMORY_GIB
    if from_start_args.boot_disk_gib is not None:
        boot_disk_gib = from_start_args.boot_disk_gib
    elif fallback.boot_disk_gib is not None:
        boot_disk_gib = fallback.boot_disk_gib
    else:
        boot_disk_gib = LIMA_DEFAULT_BOOT_DISK_GIB
    return ResolvedLimaVmSize(cpus=cpus, memory_gib=memory_gib, boot_disk_gib=boot_disk_gib)


@pure
def vm_size_from_limactl_instance(instance: Mapping[str, Any]) -> ResolvedLimaVmSize | None:
    """The size an instance runs at, from its ``limactl list --json`` entry (memory and disk are bytes there)."""
    cpus = instance.get("cpus")
    memory_bytes = instance.get("memory")
    disk_bytes = instance.get("disk")
    if not isinstance(cpus, int) or not isinstance(memory_bytes, int) or not isinstance(disk_bytes, int):
        return None
    return ResolvedLimaVmSize(
        cpus=cpus, memory_gib=memory_bytes / _BYTES_PER_GIB, boot_disk_gib=disk_bytes / _BYTES_PER_GIB
    )


@pure
def is_same_vm_size(first: ResolvedLimaVmSize, second: ResolvedLimaVmSize) -> bool:
    """Whether two sizes agree on the dimensions a running VM is configured with (CPUs and memory)."""
    return first.cpus == second.cpus and math.isclose(first.memory_gib, second.memory_gib, abs_tol=1e-6)


@pure
def host_resources_for_lima_host(vm_size: ResolvedLimaVmSize, data_disk_size: LimaDiskSize | None) -> HostResources:
    """What the host has: the VM's CPUs and memory, and the disk its data lives on.

    That is the btrfs data disk when the host has one (it backs ``host_dir`` or
    the whole home), else the boot disk.
    """
    disk_gb = data_disk_size.size_gb if data_disk_size is not None else vm_size.boot_disk_gib
    return HostResources(
        cpu=CpuResources(count=vm_size.cpus, frequency_ghz=None),
        memory_gb=vm_size.memory_gib,
        disk_gb=disk_gb,
        gpu=None,
    )


@pure
def resolved_vm_size_from_resources(resources: HostResources) -> ResolvedLimaVmSize:
    """The VM size a host's recorded resources describe (the boot disk is not recorded, so it carries lima's default)."""
    return ResolvedLimaVmSize(
        cpus=resources.cpu.count, memory_gib=resources.memory_gb, boot_disk_gib=LIMA_DEFAULT_BOOT_DISK_GIB
    )
