from typing import Any
from typing import Final

import click
from loguru import logger

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
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
from imbue.mngr.primitives import DockerCpuCount
from imbue.mngr.primitives import DockerMemorySize
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import InvalidDockerMemorySizeError
from imbue.mngr.providers.docker.backend import DOCKER_BACKEND_NAME
from imbue.mngr.providers.docker.data_types import ContainerSizeRequest
from imbue.mngr.providers.docker.instance import DockerProviderInstance

_RESIZE_APPLIED_NOTE: Final[str] = (
    "The caps are applied to the container and recorded for every later start; a gVisor container reports the "
    "new memory total in /proc/meminfo only after a restart."
)


class DockerResizeCliOptions(CommonCliOptions):
    """Options passed from the CLI to the docker resize command."""

    host: HostAddress
    cpus: int | None
    memory: str | None


@click.group(name="docker")
def docker_group() -> None:
    pass


def _build_size_request(opts: DockerResizeCliOptions) -> ContainerSizeRequest:
    """The validated size the user asked for.

    Raises click.UsageError when neither dimension is passed and UserInputError
    for a value docker could never accept.
    """
    if opts.cpus is None and opts.memory is None:
        raise click.UsageError("Nothing to resize: pass --cpus and/or --memory")
    cpus: DockerCpuCount | None = None
    if opts.cpus is not None:
        try:
            cpus = DockerCpuCount(opts.cpus)
        except InvalidPrimitiveValueError as e:
            raise UserInputError(f"--cpus must be a whole number of CPUs greater than 0, got {opts.cpus}") from e
    memory: DockerMemorySize | None = None
    if opts.memory is not None:
        try:
            memory = DockerMemorySize(opts.memory)
        except InvalidDockerMemorySizeError as e:
            raise UserInputError(
                f"--memory must be a docker memory size such as '512m' or '8g', got {opts.memory!r}"
            ) from e
    return ContainerSizeRequest(cpus=cpus, memory=memory)


@docker_group.command(name="resize")
@click.argument("host", type=HOST_ADDRESS)
@click.option(
    "--cpus",
    type=int,
    default=None,
    help="Whole CPUs to cap the container at (docker refuses more than the daemon's CPU count)",
)
@click.option(
    "--memory",
    type=str,
    default=None,
    help="Memory to cap the container at, in docker's spelling (e.g. 512m, 8g); swap is capped at the same value",
)
@add_common_options
@click.pass_context
def docker_resize(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="docker_resize",
        command_class=DockerResizeCliOptions,
    )
    logger.debug("Started docker resize command")

    request = _build_size_request(opts)
    host_ref = resolve_host_on_backend(opts.host, mngr_ctx, DOCKER_BACKEND_NAME)

    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    if not isinstance(provider, DockerProviderInstance):
        raise UserInputError(
            f"Host '{host_ref.host_name}' is on provider '{host_ref.provider_name}', which is not a docker "
            "provider; only docker hosts can be resized with 'mngr docker resize'"
        )

    resources = provider.resize_host(host_ref.host_id, request)

    emit_operator_result(
        "docker_host_resized",
        (
            OperatorResultPart.shown(
                f"Resized host {host_ref.host_name} to {resources.cpu.count} CPU(s) and {resources.memory_gb:g} GB",
                host_id=str(host_ref.host_id),
                host_name=str(host_ref.host_name),
                provider=str(host_ref.provider_name),
                cpu_count=resources.cpu.count,
                memory_gb=resources.memory_gb,
            ),
            OperatorResultPart.shown(_RESIZE_APPLIED_NOTE, note=_RESIZE_APPLIED_NOTE),
        ),
        output_opts.output_format,
    )


CommandHelpMetadata(
    key="docker",
    one_line_description="Docker-provider-specific commands",
    synopsis="mngr docker [resize] [OPTIONS]",
    description="""Commands that only make sense for hosts on the docker provider (containers
on a local or remote Docker daemon). Everything else about a docker host is
managed through the ordinary commands (create, start, stop, destroy, list).""",
    examples=(("Give a docker host 4 CPUs and 8 GB of memory", "mngr docker resize my-host --cpus 4 --memory 8g"),),
    see_also=(
        ("create", "Create an agent; -s --cpus / -s --memory set a docker host's size at creation"),
        ("list", "List agents; host.resource shows each host's recorded size"),
    ),
).register()

add_pager_help_option(docker_group)

CommandHelpMetadata(
    key="docker.resize",
    one_line_description="Change the CPU and memory caps of a docker host",
    synopsis="mngr docker resize HOST [--cpus <N>] [--memory <SIZE>]",
    arguments_description="- `HOST`: Host name or ID (optionally `HOST.PROVIDER`) of the docker host to resize.",
    description="""Rewrites the CPU and/or memory caps recorded for the host (the `--cpus` and
`--memory` flags its container was created with) and applies them to the
container with `docker update`, whether it is running or stopped. A
dimension that is not passed keeps its current value.

Docker records the caps in the container's configuration, so they survive
stop/start and daemon restarts, and mngr re-applies the recorded size on every
`mngr start` and on a snapshot restore. The cgroup caps take effect on a
running container at once; what a gVisor (runsc) container reports as its
memory total in /proc/meminfo follows only after a restart, so restart the host
if something inside it sizes itself from that total.

Docker refuses a CPU cap above the daemon's CPU count; such a value is reported
as an error and nothing is changed.

Swap is capped at the memory cap, so a container that hits its limit is shed by
the OOM killer instead of swapping the machine to a halt.""",
    examples=(
        ("Set both caps", "mngr docker resize my-host --cpus 4 --memory 8g"),
        ("Change only the memory cap", "mngr docker resize my-host --memory 16g"),
        (
            "Resize a gVisor host, then restart it so the container sees the new memory total",
            "mngr docker resize my-host --memory 16g && mngr stop my-agent --stop-host && mngr start my-agent",
        ),
    ),
    see_also=(
        ("start", "Start a stopped host (re-applies the recorded size)"),
        ("list", "List agents; host.resource shows each host's recorded size"),
    ),
).register()

add_pager_help_option(docker_resize)
