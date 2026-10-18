"""How a machine's size is chosen for each compute provider, and how each provider is told it.

Every provider spells a size its own way, so the pick travels as one
:class:`MachineSizeSelection` and is rendered here, once, into the create
flags (``mngr create``) or the resize command (``mngr docker resize``,
``mngr lima resize``) of the provider at hand. A provider with no size knob at
create (the clouds pick a named instance shape; Imbue Cloud sizes every new
machine the same) renders nothing.
"""

from typing import Final
from typing import assert_never

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.data_types import MachineSizeSelection
from imbue.minds.desktop_client.mngr_command import shell_quoted_create_flag
from imbue.minds.errors import MindError
from imbue.minds.primitives import LaunchMode

# The size a new local workspace gets unless the create form says otherwise. The
# docker and lima values mirror the template's ``[providers.docker]`` block and
# ``[create_templates.lima]`` (plus mngr_lima's 100 GiB data disk), and the modal
# values its ``[create_templates.modal]`` settings, so a form left at its defaults
# creates the machine the template would have created on its own.
DEFAULT_MACHINE_SIZE_BY_LAUNCH_MODE: Final[dict[LaunchMode, MachineSizeSelection]] = {
    LaunchMode.DOCKER: MachineSizeSelection(cpu_count=4, memory_gb=8),
    LaunchMode.LIMA: MachineSizeSelection(cpu_count=2, memory_gb=4, disk_gb=100),
    LaunchMode.MODAL: MachineSizeSelection(cpu_count=2, memory_gb=4),
}

# The provider instance names whose machines minds can resize from the settings
# page, each through that provider's own ``mngr <provider> resize`` command.
_DOCKER_PROVIDER_NAME: Final[str] = "docker"
_LIMA_PROVIDER_NAME: Final[str] = "lima"


@pure
def clamp_machine_size_to_cpu_count(selection: MachineSizeSelection, cpu_count: int) -> MachineSizeSelection:
    """``selection`` with its CPU count lowered to ``cpu_count`` when it asks for more.

    A docker daemon refuses a ``--cpus`` cap above its own CPU count, so the
    default the create form offers on a smaller machine is lowered to what
    that machine can give rather than failing the create it would submit.
    """
    if selection.cpu_count is None or selection.cpu_count <= cpu_count:
        return selection
    return MachineSizeSelection(cpu_count=cpu_count, memory_gb=selection.memory_gb, disk_gb=selection.disk_gb)


@pure
def narrow_machine_size_to_launch_mode(
    selection: MachineSizeSelection, launch_mode: LaunchMode
) -> MachineSizeSelection | None:
    """Keep only the dimensions ``launch_mode`` takes at create; None when it takes none, or none was chosen."""
    match launch_mode:
        case LaunchMode.DOCKER | LaunchMode.MODAL:
            narrowed = MachineSizeSelection(cpu_count=selection.cpu_count, memory_gb=selection.memory_gb)
        case LaunchMode.LIMA:
            narrowed = selection
        case LaunchMode.VULTR | LaunchMode.IMBUE_CLOUD | LaunchMode.AWS | LaunchMode.GCP | LaunchMode.AZURE:
            return None
        case _ as unreachable:
            assert_never(unreachable)
    return None if narrowed.is_empty else narrowed


@pure
def machine_size_create_args(launch_mode: LaunchMode, selection: MachineSizeSelection) -> list[str]:
    """The ``mngr create`` flags that give a new ``launch_mode`` machine ``selection``'s size.

    Docker and lima take their size as start args (last one wins over the
    template's defaults); lima's disk is the btrfs data disk the host's data
    lives on, a provider setting rather than a start arg; modal takes build args.
    """
    args: list[str] = []
    match launch_mode:
        case LaunchMode.DOCKER:
            if selection.cpu_count is not None:
                args.extend(shell_quoted_create_flag("-s", f"--cpus={selection.cpu_count}"))
            if selection.memory_gb is not None:
                args.extend(shell_quoted_create_flag("-s", f"--memory={selection.memory_gb}g"))
        case LaunchMode.LIMA:
            if selection.cpu_count is not None:
                args.extend(shell_quoted_create_flag("-s", f"--cpus={selection.cpu_count}"))
            if selection.memory_gb is not None:
                args.extend(shell_quoted_create_flag("-s", f"--memory={selection.memory_gb}"))
            if selection.disk_gb is not None:
                args.extend(["-S", f"providers.lima.host_data_disk_size={selection.disk_gb}GiB"])
        case LaunchMode.MODAL:
            if selection.cpu_count is not None:
                args.extend(shell_quoted_create_flag("-b", f"cpu={selection.cpu_count}"))
            if selection.memory_gb is not None:
                args.extend(shell_quoted_create_flag("-b", f"memory={selection.memory_gb}"))
        case LaunchMode.VULTR | LaunchMode.IMBUE_CLOUD | LaunchMode.AWS | LaunchMode.GCP | LaunchMode.AZURE:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return args


@pure
def is_machine_resizable_provider(provider_name: str) -> bool:
    """Whether minds can change the size of a machine on ``provider_name`` after it was created."""
    return provider_name in (_DOCKER_PROVIDER_NAME, _LIMA_PROVIDER_NAME)


@pure
def is_machine_disk_resizable_provider(provider_name: str) -> bool:
    """Whether the data disk of a machine on ``provider_name`` can be grown after it was created."""
    return provider_name == _LIMA_PROVIDER_NAME


class MachineNotResizableError(MindError):
    """Raised when a resize is rendered for a provider whose machines cannot change size after creation."""


@pure
def machine_size_resize_args(provider_name: str, host_id: str, selection: MachineSizeSelection) -> list[str]:
    """The ``mngr`` arguments that resize the host ``host_id`` on ``provider_name`` to ``selection``.

    Raises :class:`MachineNotResizableError` for a provider
    :func:`is_machine_resizable_provider` is false for.
    """
    if provider_name == _DOCKER_PROVIDER_NAME:
        args = ["docker", "resize", host_id]
        if selection.cpu_count is not None:
            args.extend(["--cpus", str(selection.cpu_count)])
        if selection.memory_gb is not None:
            args.extend(["--memory", f"{selection.memory_gb}g"])
        return args
    if provider_name == _LIMA_PROVIDER_NAME:
        args = ["lima", "resize", host_id]
        if selection.cpu_count is not None:
            args.extend(["--cpus", str(selection.cpu_count)])
        if selection.memory_gb is not None:
            args.extend(["--memory", str(selection.memory_gb)])
        if selection.disk_gb is not None:
            args.extend(["--disk", str(selection.disk_gb)])
        return args
    raise MachineNotResizableError(f"A machine on provider '{provider_name}' cannot be resized after it was created.")
