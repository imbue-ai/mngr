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
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME


def check_streaming_headless_agent_type(agent_type: str) -> type[StreamingHeadlessAgentMixin]:
    """Verify the given agent type implements StreamingHeadlessAgentMixin and return its class.

    Raises MngrError if the agent type is not registered or does not
    support streaming headless output.
    """
    agent_class = get_agent_class(agent_type)
    if not issubclass(agent_class, StreamingHeadlessAgentMixin):
        raise MngrError(
            f"The '{agent_type}' agent type does not support streaming headless output. "
            f"Only agent types implementing StreamingHeadlessAgentMixin can be used."
        )
    return agent_class


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
    except (OSError, BaseMngrError) as exc:
        logger.warning("Failed to remove work dir {}: {}", work_path, exc)


@contextmanager
def _destroy_on_exit(host: OnlineHostInterface, agent: AgentInterface) -> Iterator[None]:
    """Stop and destroy an agent on exit, suppressing cleanup errors."""
    try:
        yield
    finally:
        try:
            host.stop_agents([agent.id])
        except (OSError, BaseMngrError) as exc:
            logger.warning("Failed to stop agent {}: {}", agent.name, exc)
        try:
            host.destroy_agent(agent)
        except (OSError, BaseMngrError) as exc:
            logger.warning("Failed to destroy agent {}: {}", agent.name, exc)


@contextmanager
def ephemeral_work_location(host: OnlineHostInterface) -> Iterator[HostLocation]:
    """Yield a fresh throwaway work directory on the host; remove it on exit.

    Use this when a headless caller wants a blank scratch dir rather than an
    existing checkout (e.g. ``mngr ask``). For the common case where the
    caller is passing through to ``headless_agent_output``, wrap both::

        with ephemeral_work_location(host) as work_location:
            with headless_agent_output(source_location=work_location, ...):
                ...
    """
    work_path = create_work_dir_on_host(host)
    try:
        yield HostLocation(host=host, path=work_path)
    finally:
        remove_work_dir_on_host(host, work_path)


@contextmanager
def headless_agent_output(
    mngr_ctx: MngrContext,
    agent_type: AgentTypeName,
    source_location: HostLocation,
    agent_args: tuple[str, ...] = (),
    label_options: AgentLabelOptions | None = None,
    name: AgentName | None = None,
    initial_message: str | None = None,
    pre_create_setup: Callable[[OnlineHostInterface, Path], None] | None = None,
) -> Iterator[StreamingHeadlessAgentMixin]:
    """Create a headless agent, yield it for streaming, and destroy it on exit.

    The agent runs in-place at ``source_location.path`` on
    ``source_location.host``. The caller owns the directory's lifecycle --
    this contextmanager does not create or remove it. For a fresh throwaway
    directory, wrap with :func:`ephemeral_work_location`.

    ``initial_message`` is the caller's ``--message`` content. It is handed
    to the agent type's ``prepare_headless_work_dir`` classmethod so that
    types that drive claude via prompt files can materialise it on disk
    before the process starts. It is deliberately NOT plumbed onto
    ``CreateAgentOptions.initial_message`` -- ``api_create`` treats a
    non-None initial_message as "call ``wait_for_ready_signal`` and then
    ``send_message``", which raises on headless agents (they have no TUI to
    send keys to). The prompt reaches the agent via the on-disk prompt file
    that the agent command ``cat``s, not via ``send_message``.

    If ``pre_create_setup`` is provided, it is called with the host and work
    path after the initial-message hook runs but before the agent is created,
    allowing callers to write additional files that the agent command can
    reference.

    All filesystem operations go through the host interface so this works
    for both local and remote hosts.
    """
    agent_class = check_streaming_headless_agent_type(str(agent_type))

    host = source_location.host
    work_path = source_location.path

    agent_class.prepare_headless_work_dir(host, work_path, initial_message)
    if pre_create_setup is not None:
        pre_create_setup(host, work_path)

    agent_options = CreateAgentOptions(
        agent_type=agent_type,
        agent_args=agent_args,
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


def accumulate_chunks(chunks: Iterator[str]) -> str:
    """Accumulate all chunks from an iterator into a single string."""
    return "".join(chunks)


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
