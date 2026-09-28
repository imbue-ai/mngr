from typing import Any
from typing import Final

import click
from loguru import logger

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import HOST_ADDRESS
from imbue.mngr.cli.backend_hosts import resolve_host_on_backend
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import OperatorResultPart
from imbue.mngr.cli.output_helpers import emit_operator_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostAddress
from imbue.mngr_lima.constants import LIMA_BACKEND_NAME
from imbue.mngr_lima.data_types import LimaSizeRequest
from imbue.mngr_lima.instance import LimaProviderInstance
from imbue.mngr_lima.primitives import LimaCpuCount
from imbue.mngr_lima.primitives import LimaDiskSize
from imbue.mngr_lima.primitives import LimaMemoryGib

_RESIZE_APPLIED_NOTE: Final[str] = "The VM was stopped, so it was reconfigured to the new size at once."
_RESIZE_PENDING_NOTE: Final[str] = (
    "The VM is running, so the new size is recorded and applies at its next start (stop it and start it again)."
)


class LimaResizeCliOptions(CommonCliOptions):
    """Options passed from the CLI to the lima resize command."""

    host: HostAddress
    cpus: int | None
    memory: float | None
    disk: int | None


@click.group(name="lima")
def lima_group() -> None:
    pass


def _build_size_request(opts: LimaResizeCliOptions) -> LimaSizeRequest:
    """The validated size the user asked for.

    Raises click.UsageError when no dimension is passed and UserInputError for
    a value lima could never accept.
    """
    if opts.cpus is None and opts.memory is None and opts.disk is None:
        raise click.UsageError("Nothing to resize: pass --cpus, --memory, and/or --disk")
    cpus: LimaCpuCount | None = None
    if opts.cpus is not None:
        try:
            cpus = LimaCpuCount(opts.cpus)
        except InvalidPrimitiveValueError as e:
            raise UserInputError(f"--cpus must be a whole number of CPUs greater than 0, got {opts.cpus}") from e
    memory_gib: LimaMemoryGib | None = None
    if opts.memory is not None:
        try:
            memory_gib = LimaMemoryGib(opts.memory)
        except InvalidPrimitiveValueError as e:
            raise UserInputError(f"--memory must be a number of GiB greater than 0, got {opts.memory}") from e
    data_disk_size: LimaDiskSize | None = None
    if opts.disk is not None:
        try:
            disk_gib = PositiveInt(opts.disk)
        except InvalidPrimitiveValueError as e:
            raise UserInputError(f"--disk must be a whole number of GiB greater than 0, got {opts.disk}") from e
        data_disk_size = LimaDiskSize(f"{disk_gib}GiB")
    return LimaSizeRequest(cpus=cpus, memory_gib=memory_gib, data_disk_size=data_disk_size)


@lima_group.command(name="resize")
@click.argument("host", type=HOST_ADDRESS)
@click.option("--cpus", type=int, default=None, help="Whole CPUs to give the VM")
@click.option("--memory", type=float, default=None, help="RAM to give the VM, in GiB (e.g. 8 or 1.5)")
@click.option(
    "--disk",
    type=int,
    default=None,
    help="Size to grow the btrfs data disk to, in GiB; a value below the current size is refused",
)
@add_common_options
@click.pass_context
def lima_resize(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="lima_resize",
        command_class=LimaResizeCliOptions,
    )
    logger.debug("Started lima resize command")

    request = _build_size_request(opts)
    host_ref = resolve_host_on_backend(opts.host, mngr_ctx, LIMA_BACKEND_NAME)

    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    if not isinstance(provider, LimaProviderInstance):
        raise UserInputError(
            f"Host '{host_ref.host_name}' is on provider '{host_ref.provider_name}', which is not a lima "
            "provider; only lima hosts can be resized with 'mngr lima resize'"
        )

    outcome = provider.resize_host(host_ref.host_id, request)
    resources = outcome.resources
    note = _RESIZE_APPLIED_NOTE if outcome.is_applied_to_instance else _RESIZE_PENDING_NOTE
    disk_text = f"{resources.disk_gb:g} GiB of disk" if resources.disk_gb is not None else "an unreported disk"

    emit_operator_result(
        "lima_host_resized",
        (
            OperatorResultPart.shown(
                f"Resized host {host_ref.host_name} to {resources.cpu.count} CPU(s), "
                f"{resources.memory_gb:g} GiB of memory, and {disk_text}",
                host_id=str(host_ref.host_id),
                host_name=str(host_ref.host_name),
                provider=str(host_ref.provider_name),
                cpu_count=resources.cpu.count,
                memory_gb=resources.memory_gb,
                disk_gb=resources.disk_gb,
                is_restart_needed_to_apply=not outcome.is_applied_to_instance,
            ),
            OperatorResultPart.shown(note, note=note),
        ),
        output_opts.output_format,
    )


CommandHelpMetadata(
    key="lima",
    one_line_description="Lima-provider-specific commands",
    synopsis="mngr lima [resize] [OPTIONS]",
    description="""Commands that only make sense for hosts on the lima provider (local Lima VMs).
Everything else about a lima host is managed through the ordinary commands
(create, start, stop, destroy, list).""",
    examples=(
        (
            "Give a lima host 4 CPUs, 8 GiB of memory, and a 200 GiB data disk",
            "mngr lima resize my-host --cpus 4 --memory 8 --disk 200",
        ),
    ),
    see_also=(
        ("create", "Create an agent; -s --cpus / -s --memory / -s --disk set a lima host's size at creation"),
        ("list", "List agents; host.resource shows each host's recorded size"),
    ),
).register()

add_pager_help_option(lima_group)

CommandHelpMetadata(
    key="lima.resize",
    one_line_description="Change the CPUs, memory, or data-disk size of a lima host",
    synopsis="mngr lima resize HOST [--cpus <N>] [--memory <GIB>] [--disk <GIB>]",
    arguments_description="- `HOST`: Host name or ID (optionally `HOST.PROVIDER`) of the lima host to resize.",
    description="""Rewrites the size recorded for the host and applies it to its VM. A dimension
that is not passed keeps its current value.

The CPU count and memory are the VM's `--cpus` and `--memory`; they are written
into the instance's lima config. `--disk` is the btrfs data disk that backs the
host's data (not the VM's boot disk), grown with `limactl disk resize`; the
filesystem grows to fill it on the VM's next boot. A disk never shrinks, and a
host created with the exposed bind-mount layout has no data disk to resize.

A stopped VM is reconfigured at once. A running VM cannot be edited, so the
new size is recorded and applied when the host is next started; `mngr start`
always brings the VM up to the recorded size first. A value lima refuses is
reported as an error and changes nothing.""",
    examples=(
        ("Set all three dimensions", "mngr lima resize my-host --cpus 4 --memory 8 --disk 200"),
        ("Grow only the data disk", "mngr lima resize my-host --disk 300"),
        (
            "Resize a running host, then restart it so the VM boots at the new size",
            "mngr lima resize my-host --memory 16 && mngr stop my-agent --stop-host && mngr start my-agent",
        ),
    ),
    see_also=(
        ("start", "Start a stopped host (applies the recorded size first)"),
        ("list", "List agents; host.resource shows each host's recorded size"),
    ),
).register()

add_pager_help_option(lima_resize)
