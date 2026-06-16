import shlex
import sys
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import assert_never

from loguru import logger

from imbue.mngr.api.create import create as api_create
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import OutputFormat


def is_streaming_headless_agent_type(agent_type: str, config: MngrConfig) -> bool:
    """Return True if the given agent type implements StreamingHeadlessAgentMixin.

    Resolves via ``resolve_agent_type`` so this returns the correct answer
    for both directly-registered types and TOML-defined custom types that
    inherit through ``parent_type``. Raises ``UnknownAgentTypeError`` if the
    type name is not known anywhere.
    """
    resolved = resolve_agent_type(AgentTypeName(agent_type), config)
    return issubclass(resolved.agent_class, StreamingHeadlessAgentMixin)


def check_streaming_headless_agent_type(agent_type: str, config: MngrConfig) -> None:
    """Verify the agent type resolves to a class implementing StreamingHeadlessAgentMixin.

    Raises ``UnknownAgentTypeError`` if the type is not known, or ``MngrError``
    if the resolved class does not implement StreamingHeadlessAgentMixin.
    """
    if not is_streaming_headless_agent_type(agent_type, config):
        raise MngrError(
            f"The '{agent_type}' agent type does not support streaming headless output. "
            f"Only agent types implementing StreamingHeadlessAgentMixin can be used."
        )


def create_work_dir_on_host(host: OnlineHostInterface) -> Path:
    """Create a temporary work directory on the host and return its path."""
    result = host.execute_stateful_command("mktemp -d /tmp/mngr-headless-XXXXXXXXXX")
    if not result.success:
        raise MngrError(f"Failed to create temp directory on host: {result.stderr}")
    return Path(result.stdout.strip())


def remove_work_dir_on_host(host: OnlineHostInterface, work_path: Path) -> None:
    """Remove a work directory on the host, best-effort.

    Logs a warning (but does not raise) on transport-level failures or
    non-zero exit codes so cleanup errors are visible without breaking
    the main flow. execute_idempotent_command returns a CommandResult and
    does not raise on non-zero exit, so explicitly check result.success.
    """
    try:
        result = host.execute_idempotent_command(f"rm -rf {shlex.quote(str(work_path))}")
    except (OSError, MngrError) as exc:
        logger.warning("Failed to remove work dir {}: {}", work_path, exc)
        return
    if not result.success:
        detail = result.stderr.strip() or result.stdout.strip()
        logger.warning("Failed to remove work dir {}: {}", work_path, detail)


@contextmanager
def destroy_agent_on_exit(host: OnlineHostInterface, agent: AgentInterface) -> Iterator[None]:
    """Stop and destroy an agent on exit, suppressing cleanup errors."""
    try:
        yield
    finally:
        try:
            host.stop_agents([agent.id])
        except (OSError, MngrError, CleanupFailedGroup) as exc:
            logger.warning("Failed to stop agent {}: {}", agent.name, exc)
        try:
            host.destroy_agent(agent)
        except (OSError, MngrError, CleanupFailedGroup) as exc:
            logger.warning("Failed to destroy agent {}: {}", agent.name, exc)


@contextmanager
def ephemeral_work_location(host: OnlineHostInterface) -> Iterator[HostLocation]:
    """Yield a fresh throwaway work directory on the host; remove it on exit.

    Use this when a headless caller wants a blank scratch dir rather than an
    existing checkout (e.g. ``mngr ask``). For the common case where the
    caller is passing through to ``headless_agent_output``, stack both in a
    single compound ``with`` statement::

        with (
            ephemeral_work_location(host) as work_location,
            headless_agent_output(source_location=work_location, ...) as agent,
        ):
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

    ``initial_message`` is the caller's ``--message`` content. It is stored
    on the agent via ``CreateAgentOptions.initial_message`` like on the
    non-headless path, and ``api_create`` dispatches it through
    ``agent.stage_initial_message`` (which writes the prompt into the
    agent's state dir) before starting the agent. Headless agents cannot
    receive messages via ``send_message``, so ``api_create`` short-circuits
    its wait-for-ready + send dance for any ``StreamingHeadlessAgentMixin``.

    If ``pre_create_setup`` is provided, it is called with the host and work
    path before the agent is created, allowing callers to write additional
    files into the work dir that the agent command can reference (e.g.
    ``mngr ask`` stages its system prompt this way).

    All filesystem operations go through the host interface so this works
    for both local and remote hosts.
    """
    check_streaming_headless_agent_type(str(agent_type), mngr_ctx.config)

    host = source_location.host
    work_path = source_location.path

    if pre_create_setup is not None:
        pre_create_setup(host, work_path)

    agent_options = CreateAgentOptions(
        agent_type=agent_type,
        agent_args=agent_args,
        label_options=label_options or AgentLabelOptions(),
        target_path=work_path,
        name=name,
        initial_message=initial_message,
    )

    result = api_create(
        source_location=source_location,
        target_host=host,
        agent_options=agent_options,
        mngr_ctx=mngr_ctx,
        create_work_dir=False,
    )

    agent = result.agent
    with destroy_agent_on_exit(host, agent):
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
            write_json_line({"response": response})
        case OutputFormat.JSONL:
            response = accumulate_chunks(chunks)
            write_json_line({"event": "response", "response": response})
        case _ as unreachable:
            assert_never(unreachable)
