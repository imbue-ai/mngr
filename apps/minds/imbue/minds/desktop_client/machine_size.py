"""How a machine's size is chosen for each compute provider, and how each provider is told it.

Every provider spells a size its own way, so the pick travels as one
:class:`MachineSizeSelection` and is rendered here, once, into the create
flags (``mngr create``) or the resize command (``mngr docker resize``,
``mngr lima resize``) of the provider at hand. A provider with no size knob at
create (the clouds pick a named instance shape; Imbue Cloud sizes every new
machine the same) renders nothing.

An Imbue Cloud machine is resized after creation through the connector (the
``ImbueCloudCli`` wrapper records the size), not through a command rendered
here. What lives here for it is the plan gate that decides which accounts are
offered the size controls, and the headroom arithmetic that mirrors the
connector's quota checks so the page only offers sizes the connector would
grant.
"""

from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.data_types import MachineSizeSelection
from imbue.minds.desktop_client.mngr_command import shell_quoted_create_flag
from imbue.minds.errors import MindError
from imbue.minds.primitives import LaunchMode
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MACHINE_UNITS_STEP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MAX_MACHINE_UNITS
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus

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

# The provider instance names whose machines are resized through that provider's
# own ``mngr <provider> resize`` command (an Imbue Cloud machine is resized from
# the same settings page, but through the connector; see the module docstring).
_DOCKER_PROVIDER_NAME: Final[str] = "docker"
_LIMA_PROVIDER_NAME: Final[str] = "lima"

# The Imbue Cloud plans whose accounts are offered the machine-size controls
# for their leased machines. The connector's quotas bound every plan the same
# way; this gate only decides who sees the controls.
# CLEANUP: drop the gate (offer the controls to every plan) once Imbue Cloud
# machine resizing is opened up beyond the ally plan.
IMBUE_CLOUD_RESIZE_PLAN_NAMES: Final[frozenset[str]] = frozenset({"ally"})

# The lifecycle statuses a running Imbue Cloud machine's units are counted
# under by the connector's ``max_active_machine_units`` quota.
_IMBUE_CLOUD_RUNNING_STATUSES: Final[frozenset[WorkspaceStatus]] = frozenset(
    (WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPING, WorkspaceStatus.STARTING)
)
# The statuses during which a resize is refused (starting) or would race the
# start that applies it (stopping).
_IMBUE_CLOUD_TRANSITIONING_STATUSES: Final[frozenset[WorkspaceStatus]] = frozenset(
    (WorkspaceStatus.STOPPING, WorkspaceStatus.STARTING)
)
# The statuses whose data disk the connector counts under ``max_total_machine_disk_gb``.
_IMBUE_CLOUD_DISK_COUNTED_STATUSES: Final[frozenset[WorkspaceStatus]] = _IMBUE_CLOUD_RUNNING_STATUSES | {
    WorkspaceStatus.STOPPED
}


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
    """Whether a machine on ``provider_name`` is resized through that provider's own ``mngr <provider> resize``.

    False for an Imbue Cloud machine, which the settings page resizes too,
    but through the connector rather than a provider command.
    """
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


@pure
def is_imbue_cloud_resize_offered_to_plan(plan_name: str) -> bool:
    """Whether the settings page offers the size controls to an account on ``plan_name``."""
    return plan_name in IMBUE_CLOUD_RESIZE_PLAN_NAMES


class ImbueCloudMachineQuotaUsage(FrozenModel):
    """The two machine quotas of an Imbue Cloud account with their live usage, as ``GET /account`` reports them."""

    max_active_machine_units: int = Field(description="Units the plan allows summed across running machines")
    active_machine_units: int = Field(
        description="Units in use across running machines, each counted at the larger of its current and pending size"
    )
    max_total_machine_disk_gb: int = Field(
        description="Data-disk GB the plan allows across running + stopped machines"
    )
    total_machine_disk_gb: int = Field(
        description="Data-disk GB in use across running + stopped machines, each at the larger of current and pending"
    )


class ImbueCloudResizeBounds(FrozenModel):
    """How large one Imbue Cloud machine may be made without a quota refusal, and why."""

    memory_choices_gb: tuple[int, ...] = Field(
        description="The RAM sizes the machine may be set to, ascending; its current size is always among them"
    )
    is_memory_capped_by_plan: bool = Field(
        description="Whether the plan's units quota, not the fleet's largest size, bounds the choices"
    )
    plan_memory_limit_gb: int = Field(description="The plan's units quota across running machines")
    other_machines_memory_gb: int = Field(description="Units the account's other running machines use")
    max_disk_gb: int = Field(
        description="The largest data disk the machine may be grown to (its current size when none)"
    )
    plan_disk_limit_gb: int = Field(description="The plan's data-disk quota across running + stopped machines")
    other_machines_disk_gb: int = Field(description="Data-disk GB the account's other counted machines use")


@pure
def _counted_size(current: int, target: int | None) -> int:
    """What the connector counts a machine at: the larger of its current and pending size."""
    return max(current, target) if target is not None else current


@pure
def compute_imbue_cloud_resize_bounds(
    status: WorkspaceStatus,
    memory_units: int,
    target_memory_units: int | None,
    disk_gb: int,
    target_disk_gb: int | None,
    quotas: ImbueCloudMachineQuotaUsage,
) -> ImbueCloudResizeBounds:
    """The sizes a machine may be given that its account's quotas would grant.

    Mirrors the connector's arithmetic: a running machine already counts
    toward the active-units sum at the larger of its current and pending
    size, so its own share is handed back before the headroom is measured; a
    stopped machine counts nothing until its start re-enters it. Disk is
    counted over running and stopped machines alike and never shrinks, so the
    floor is the machine's current disk.
    """
    # Units: what the other running machines leave of the plan's quota.
    own_counted_units = (
        _counted_size(memory_units, target_memory_units) if status in _IMBUE_CLOUD_RUNNING_STATUSES else 0
    )
    other_machines_units = max(0, quotas.active_machine_units - own_counted_units)
    units_headroom = quotas.max_active_machine_units - other_machines_units
    largest_by_quota = (units_headroom // MACHINE_UNITS_STEP) * MACHINE_UNITS_STEP
    largest_offered = min(MAX_MACHINE_UNITS, largest_by_quota)
    # A running machine the quota no longer holds (a plan lowered below usage)
    # is refused every change, a shrink included, because the connector counts
    # it at the larger of its current and target size; a stopped one may still
    # shrink to what its start would be granted. Keeping the current size is
    # never a grant, so it is always offered.
    is_held_at_current_size = status in _IMBUE_CLOUD_RUNNING_STATUSES and memory_units > units_headroom
    if is_held_at_current_size:
        choices = {memory_units}
    else:
        choices = {
            units for units in range(MACHINE_UNITS_STEP, largest_offered + MACHINE_UNITS_STEP, MACHINE_UNITS_STEP)
        }
        choices.add(memory_units)

    # Disk: what the other counted machines leave, floored at the current disk.
    own_counted_disk_gb = _counted_size(disk_gb, target_disk_gb) if status in _IMBUE_CLOUD_DISK_COUNTED_STATUSES else 0
    other_machines_disk_gb = max(0, quotas.total_machine_disk_gb - own_counted_disk_gb)
    disk_headroom_gb = quotas.max_total_machine_disk_gb - other_machines_disk_gb
    return ImbueCloudResizeBounds(
        memory_choices_gb=tuple(sorted(choices)),
        is_memory_capped_by_plan=largest_by_quota < MAX_MACHINE_UNITS,
        plan_memory_limit_gb=quotas.max_active_machine_units,
        other_machines_memory_gb=other_machines_units,
        max_disk_gb=max(disk_gb, disk_headroom_gb),
        plan_disk_limit_gb=quotas.max_total_machine_disk_gb,
        other_machines_disk_gb=other_machines_disk_gb,
    )


@pure
def is_imbue_cloud_machine_transitioning(status: WorkspaceStatus) -> bool:
    """Whether the machine is mid stop or start, when a resize is refused or would race the start applying it."""
    return status in _IMBUE_CLOUD_TRANSITIONING_STATUSES


@pure
def is_imbue_cloud_machine_running(status: WorkspaceStatus) -> bool:
    return status is WorkspaceStatus.RUNNING
