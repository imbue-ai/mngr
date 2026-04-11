import sys
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import assert_never

from loguru import logger

from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import HostLocation
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME


def check_streaming_headless_agent_type(agent_type: str) -> None:
    """Verify the given agent type implements StreamingHeadlessAgentMixin.

    Raises MngrError if the agent type is not registered or does not
    support streaming headless output.
    """
    agent_class = get_agent_class(agent_type)
    if not issubclass(agent_class, StreamingHeadlessAgentMixin):
        raise MngrError(
            f"The '{agent_type}' agent type does not support streaming headless output. "
            f"Only agent types implementing StreamingHeadlessAgentMixin can be used."
        )


def get_local_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Resolve the local host as an OnlineHostInterface."""
    provider = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx)
    host_interface = provider.get_host(HostName(LOCAL_HOST_NAME))
    if not isinstance(host_interface, OnlineHostInterface):
        raise MngrError("Local host is not online")
    return host_interface


def create_work_dir_on_host(host: OnlineHostInterface) -> Path:
    """Create a temporary work directory on the host and return its path."""
    result = host.execute_stateful_command("mktemp -d /tmp/mngr-headless-XXXXXXXXXX")
    if not result.success:
        raise MngrError(f"Failed to create temp directory on host: {result.stderr}")
    return Path(result.stdout.strip())


def remove_work_dir_on_host(host: OnlineHostInterface, work_path: Path) -> None:
    """Remove a work directory on the host, suppressing errors."""
    try:
        host.execute_idempotent_command(f"rm -rf '{work_path}'")
    except (OSError, BaseMngrError):
        logger.debug("Failed to remove work dir {}", work_path)


@contextmanager
def _destroy_on_exit(host: OnlineHostInterface, agent: AgentInterface) -> Iterator[None]:
    """Stop and destroy an agent on exit, suppressing cleanup errors."""
    try:
        yield
    finally:
        try:
            host.stop_agents([agent.id])
        except (OSError, BaseMngrError):
            logger.debug("Failed to stop agent {}", agent.name)
        try:
            host.destroy_agent(agent)
        except (OSError, BaseMngrError):
            logger.debug("Failed to destroy agent {}", agent.name)


@contextmanager
def headless_agent_output(
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    agent_type: AgentTypeName,
    agent_args: tuple[str, ...] = (),
    command: CommandString | None = None,
    label_options: AgentLabelOptions | None = None,
    name: AgentName | None = None,
    pre_create_setup: Callable[[OnlineHostInterface, Path], None] | None = None,
) -> Iterator[StreamingHeadlessAgentMixin]:
    """Create a headless agent, yield it for streaming, and destroy it on exit.

    Creates a temporary directory on the host as the work path (no git branch
    creation). If ``pre_create_setup`` is provided, it is called with the host
    and work path before the agent is created, allowing callers to write files
    that the agent command can reference.

    All filesystem operations go through the host interface so this works
    for both local and remote hosts.
    """
    check_streaming_headless_agent_type(str(agent_type))

    work_path = create_work_dir_on_host(host)
    try:
        if pre_create_setup is not None:
            pre_create_setup(host, work_path)

        source_location = HostLocation(host=host, path=work_path)
        agent_options = CreateAgentOptions(
            agent_type=agent_type,
            agent_args=agent_args,
            command=command,
            label_options=label_options or AgentLabelOptions(),
            target_path=work_path,
            name=name,
        )

        result = api_create(
            source_location=source_location,
            target_host=host,
            agent_options=agent_options,
            mngr_ctx=mngr_ctx,
            create_work_dir=False,
        )

        agent = result.agent
        with _destroy_on_exit(host, agent):
            if not isinstance(agent, StreamingHeadlessAgentMixin):
                raise MngrError(f"Expected streaming headless agent, got {type(agent).__name__}")
            yield agent
    finally:
        remove_work_dir_on_host(host, work_path)


def accumulate_chunks(chunks: Iterator[str]) -> str:
    """Accumulate all chunks from an iterator into a single string."""
    parts: list[str] = []
    for chunk in chunks:
        parts.append(chunk)
    return "".join(parts)


def stream_or_accumulate_response(chunks: Iterator[str], output_format: OutputFormat) -> None:
    """Stream response chunks for HUMAN format, or accumulate for JSON/JSONL."""
    match output_format:
        case OutputFormat.HUMAN:
            for chunk in chunks:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            sys.stdout.write("\n")
            sys.stdout.flush()
        case OutputFormat.JSON:
            response = accumulate_chunks(chunks)
            emit_final_json({"response": response})
        case OutputFormat.JSONL:
            response = accumulate_chunks(chunks)
            emit_final_json({"event": "response", "response": response})
        case _ as unreachable:
            assert_never(unreachable)
