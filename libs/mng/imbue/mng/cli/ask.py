import shlex
import subprocess
import sys
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.api.create import create as api_create
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.help_formatter import get_all_help_metadata
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import BaseMngError
from imbue.mng.errors import MngError
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import OutputFormat

_QUERY_PREFIX: Final[str] = (
    "answer this question about `mng`. "
    "respond concisely with the mng command(s) and a brief explanation. "
    "no markdown formatting. "
    "here are some example questions and ideal responses:\n\n"
    #
    "user: How do I create a container on modal with custom packages installed by default?\n"
    "response: Simply run:\n"
    '    mng create @.modal -b "--file path/to/Dockerfile"\n'
    "If you don't have a Dockerfile for your project, run:\n"
    "    mng bootstrap\n"
    "from the repo where you would like a Dockerfile created.\n\n"
    #
    "user: How do I spin up 5 agents on the cloud?\n"
    "response: mng create --provider modal\n"
    "Run the command 5 times (once per agent). Each will get a unique auto-generated name.\n\n"
    #
    "user: How do I run multiple agents on the same cloud machine to save costs?\n"
    "response: Create them on a shared host:\n"
    "    mng create agent-1@shared-host.modal --new-host\n"
    "    mng create agent-2@shared-host\n\n"
    #
    "user: How do I launch an agent with a task without connecting to it?\n"
    'response: mng create --no-connect -m "fix all failing tests and commit"\n\n'
    #
    "user: How do I send the same message to all my running agents?\n"
    'response: mng message --all -m "rebase on main and resolve any conflicts"\n\n'
    #
    "user: How do I send a long task description from a file to an agent?\n"
    "response: Pipe it from stdin:\n"
    "    cat spec.md | mng message my-agent\n\n"
    #
    "user: How do I continuously sync files between my machine and a remote agent?\n"
    "response: mng pair my-agent\n\n"
    #
    "user: How do I pull an agent's git commits back to my local repo?\n"
    "response: mng pull my-agent --sync-mode git\n\n"
    #
    "user: How do I push my local changes to a running agent?\n"
    "response: mng push my-agent\n\n"
    #
    "user: How do I clone an existing agent to try something risky?\n"
    "response: mng clone my-agent experiment\n\n"
    #
    "user: How do I see what agents would be stopped without actually stopping them?\n"
    "response: mng stop --all --dry-run\n\n"
    #
    "user: How do I destroy all my agents?\n"
    "response: mng destroy --all --force\n\n"
    #
    "user: How do I create an agent with environment secrets?\n"
    "response: mng create --env-file .env.secrets\n\n"
    #
    "user: How do I create an agent from a saved template?\n"
    "response: mng create --template gpu-heavy\n\n"
    #
    "user: How do I run a test watcher alongside my agent?\n"
    "response: Use --extra-window (or -w) to open an extra tmux window:\n"
    '    mng create -w "watch -n5 pytest"\n\n'
    #
    "user: How do I get a list of running agent names as JSON?\n"
    "response: mng list --running --format json\n\n"
    #
    "user: How do I watch agent status in real time?\n"
    "response: mng list --watch 5\n\n"
    #
    "user: How do I list all agents that are running or waiting?\n"
    "response: Use a CEL filter on the `state` field:\n"
    '    mng list --include \'state == "RUNNING" || state == "WAITING"\'\n\n'
    #
    "user: How do I list agents whose name contains 'prod'?\n"
    "response: mng list --include 'name.contains(\"prod\")'\n\n"
    #
    "user: How do I find agents that have been idle for more than an hour?\n"
    "response: mng list --include 'idle_seconds > 3600'\n\n"
    #
    "user: How do I list running agents on Modal?\n"
    'response: mng list --include \'state == "RUNNING" && host.provider == "modal"\'\n'
    "To include agents that are waiting for input, add WAITING:\n"
    '    mng list --include \'(state == "RUNNING" || state == "WAITING") && host.provider == "modal"\'\n\n'
    #
    "user: How do I list agents for the mng project?\n"
    "response: mng list --include 'labels.project == \"mng\"'\n\n"
    #
    "user: How do I message only agents with a specific tag?\n"
    "response: Use a CEL filter:\n"
    '    mng message --include \'tags.feature == "auth"\' -m "run the auth test suite"\n\n'
    #
    "user: How do I launch 3 independent tasks in parallel on the cloud?\n"
    "response: Run multiple creates with --no-connect:\n"
    '    mng create @.modal --no-connect -m "implement dark mode"\n'
    '    mng create @.modal --no-connect -m "add i18n support"\n'
    '    mng create @.modal --no-connect -m "optimize database queries"\n\n'
    #
    "user: How do I launch an agent on Modal?\n"
    "response: mng create @.modal\n\n"
    #
    "user: How do I launch an agent locally?\n"
    "response: mng create\n\n"
    #
    "user: How do I create an agent with a specific name?\n"
    "response: mng create my-task\n\n"
    #
    "user: How do I use codex instead of claude?\n"
    "response: mng create my-task codex\n\n"
    #
    "user: How do I pass arguments to the underlying agent, like choosing a model?\n"
    "response: Use -- to separate mng args from agent args:\n"
    "    mng create -- --model opus\n\n"
    #
    "user: How do I connect to an existing agent?\n"
    "response: mng connect my-agent\n\n"
    #
    "user: How do I see all my agents?\n"
    "response: mng list\n\n"
    #
    "user: How do I see only running agents?\n"
    "response: mng list --running\n\n"
    #
    "user: How do I stop a specific agent?\n"
    "response: mng stop my-agent\n\n"
    #
    "user: How do I stop all running agents?\n"
    "response: mng stop --all\n\n"
    #
    "now answer this user's question:\n"
    "user: "
)

_EXECUTE_QUERY_PREFIX: Final[str] = (
    "answer this question about `mng`. "
    "respond with ONLY the valid mng command, with no markdown formatting, explanation, or extra text. "
    "the output will be executed directly as a shell command. "
    "here are some example questions and ideal responses:\n\n"
    #
    "user: spin up 5 agents on the cloud\n"
    "response: mng create @.modal\n"
    "Run the command 5 times (once per agent).\n\n"
    #
    "user: send all agents a message to rebase on main\n"
    'response: mng message --all -m "rebase on main and resolve any conflicts"\n\n'
    #
    "user: stop all running agents\n"
    "response: mng stop --all\n\n"
    #
    "user: destroy everything\n"
    "response: mng destroy --all --force\n\n"
    #
    "user: create a cloud agent that immediately starts fixing tests\n"
    'response: mng create @.modal --no-connect -m "fix all failing tests and commit"\n\n'
    #
    "user: list running agents as json\n"
    "response: mng list --running --format json\n\n"
    #
    "user: list all agents that are running or waiting\n"
    'response: mng list --include \'state == "RUNNING" || state == "WAITING"\'\n\n'
    #
    "user: list agents whose name contains prod\n"
    "response: mng list --include 'name.contains(\"prod\")'\n\n"
    #
    "user: find agents idle for more than an hour\n"
    "response: mng list --include 'idle_seconds > 3600'\n\n"
    #
    "user: list running agents on modal\n"
    'response: mng list --include \'state == "RUNNING" && host.provider == "modal"\'\n\n'
    #
    "user: list agents for the mng project\n"
    "response: mng list --include 'labels.project == \"mng\"'\n\n"
    #
    "user: clone my-agent into a new agent called experiment\n"
    "response: mng clone my-agent experiment\n\n"
    #
    "user: pull git commits from my-agent\n"
    "response: mng pull my-agent --sync-mode git\n\n"
    #
    "user: create a local agent with opus\n"
    "response: mng create -- --model opus\n\n"
    #
    "now respond with ONLY the mng command for this request:\n"
    "user: "
)


class ClaudeBackendInterface(MutableModel, ABC):
    """Abstraction over the claude backend for testability."""

    @abstractmethod
    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        """Send a prompt to claude and yield response text chunks."""


@contextmanager
def _destroy_on_exit(host: OnlineHostInterface, agent: AgentInterface) -> Iterator[None]:
    """Stop and destroy an agent on exit, suppressing cleanup errors."""
    try:
        yield
    finally:
        try:
            host.stop_agents([agent.id])
        except (OSError, BaseMngError):
            logger.debug("Failed to stop ask agent {}", agent.name)
        try:
            host.destroy_agent(agent)
        except (OSError, BaseMngError):
            logger.debug("Failed to destroy ask agent {}", agent.name)


def _get_local_host(mng_ctx: MngContext) -> OnlineHostInterface:
    """Resolve the local host as an OnlineHostInterface."""
    provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
    host_interface = provider.get_host(HostName("localhost"))
    if not isinstance(host_interface, OnlineHostInterface):
        raise MngError("Local host is not online")
    return host_interface


def _create_work_dir_on_host(host: OnlineHostInterface) -> Path:
    """Create a temporary work directory on the host and return its path."""
    result = host.execute_command("mktemp -d /tmp/mng-ask-XXXXXXXXXX")
    if not result.success:
        raise MngError(f"Failed to create temp directory on host: {result.stderr}")
    return Path(result.stdout.strip())


def _remove_work_dir_on_host(host: OnlineHostInterface, work_path: Path) -> None:
    """Remove a work directory on the host, suppressing errors."""
    try:
        host.execute_command(f"rm -rf '{work_path}'")
    except (OSError, BaseMngError):
        logger.debug("Failed to remove ask work dir {}", work_path)


def _check_headless_claude_available() -> None:
    """Verify the headless_claude plugin is available.

    When mng is installed as a standalone tool (not via ``uv run``), the
    mng_claude plugin may not be present, causing a silent fallback to
    BaseAgent which doesn't support streaming output.
    """
    agent_class = get_agent_class("headless_claude")
    if not issubclass(agent_class, StreamingHeadlessAgentMixin):
        raise MngError(
            "The 'headless_claude' agent type is not available. "
            "The mng_claude plugin may not be installed.\n"
            "Install it with:\n"
            "  mng plugin add mng-claude"
        )


@contextmanager
def _headless_claude_output(
    host: OnlineHostInterface, mng_ctx: MngContext, prompt: str, system_prompt: str
) -> Iterator[StreamingHeadlessAgentMixin]:
    """Create a HeadlessClaude agent, yield it, and destroy it on exit.

    Creates a temporary directory on the host as the work path (no git branch
    creation), and passes claude args for headless operation (--system-prompt,
    --output-format stream-json, --tools "", --no-session-persistence).

    All filesystem operations go through the host interface so this works
    for both local and remote hosts.
    """
    _check_headless_claude_available()

    work_path = _create_work_dir_on_host(host)
    try:
        # Write prompt and system prompt to files via the host interface so
        # the shell command can read them via $(cat ...).  Passing them inline
        # as agent_args would exceed tmux's command length limit.
        host.write_text_file(work_path / ".mng-system-prompt", system_prompt)
        host.write_text_file(work_path / ".mng-prompt", prompt)

        agent_args = (
            "--system-prompt",
            '"$(cat "$MNG_AGENT_WORK_DIR/.mng-system-prompt")"',
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            '""',
            "--no-session-persistence",
            '"$(cat "$MNG_AGENT_WORK_DIR/.mng-prompt")"',
        )

        source_location = HostLocation(host=host, path=work_path)
        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("headless_claude"),
            agent_args=agent_args,
            label_options=AgentLabelOptions(labels={"internal": "ask"}),
            target_path=work_path,
            name=AgentName("ask"),
        )

        result = api_create(
            source_location=source_location,
            target_host=host,
            agent_options=agent_options,
            mng_ctx=mng_ctx,
            create_work_dir=False,
        )

        agent = result.agent
        with _destroy_on_exit(host, agent):
            if not isinstance(agent, StreamingHeadlessAgentMixin):
                raise MngError(f"Expected streaming headless agent, got {type(agent).__name__}")
            yield agent
    finally:
        _remove_work_dir_on_host(host, work_path)


class HeadlessClaudeBackend(ClaudeBackendInterface):
    """Runs claude via a HeadlessClaude agent for proper agent lifecycle management."""

    host: OnlineHostInterface
    mng_ctx: MngContext

    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        with _headless_claude_output(self.host, self.mng_ctx, prompt, system_prompt) as agent:
            yield from agent.stream_output()


def _accumulate_chunks(chunks: Iterator[str]) -> str:
    """Accumulate all chunks from an iterator into a single string."""
    parts: list[str] = []
    for chunk in chunks:
        parts.append(chunk)
    return "".join(parts)


def _build_ask_context() -> str:
    """Build system prompt context from the registered help metadata.

    Constructs a documentation string from the in-memory help metadata
    registry, so no pre-generated files are needed.
    """
    parts: list[str] = [
        "# mng CLI Documentation",
        "",
        "mng is a tool for managing AI coding agents across different hosts.",
        "",
    ]

    for name, metadata in get_all_help_metadata().items():
        parts.append(f"## mng {name}")
        parts.append("")
        parts.append(f"Synopsis: {metadata.synopsis}")
        parts.append("")
        parts.append(metadata.full_description.strip())
        parts.append("")
        if metadata.examples:
            parts.append("Examples:")
            for desc, cmd in metadata.examples:
                parts.append(f"  {desc}: {cmd}")
            parts.append("")

    return "\n".join(parts)


def _show_command_summary(output_format: OutputFormat) -> None:
    """Show a summary of available mng commands."""
    metadata = get_all_help_metadata()
    match output_format:
        case OutputFormat.HUMAN:
            write_human_line("Available mng commands:\n")
            for name, meta in metadata.items():
                write_human_line("  mng {:<12} {}", name, meta.one_line_description)
            write_human_line('\nAsk a question: mng ask "how do I create an agent?"')
        case OutputFormat.JSON:
            commands = {name: meta.one_line_description for name, meta in metadata.items()}
            emit_final_json({"commands": commands})
        case OutputFormat.JSONL:
            commands = {name: meta.one_line_description for name, meta in metadata.items()}
            emit_final_json({"event": "commands", "commands": commands})
        case _ as unreachable:
            assert_never(unreachable)


class AskCliOptions(CommonCliOptions):
    """Options passed from the CLI to the ask command."""

    query: tuple[str, ...]
    execute: bool


@click.command(name="ask")
@click.argument("query", nargs=-1, required=False)
@optgroup.group("Behavior")
@optgroup.option(
    "--execute",
    is_flag=True,
    help="Execute the generated CLI command instead of just printing it",
)
@add_common_options
@click.pass_context
def ask(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _ask_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _ask_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of ask command (extracted for exception handling)."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="ask",
        command_class=AskCliOptions,
    )
    logger.debug("Started ask command")

    if not opts.query:
        _show_command_summary(output_opts.output_format)
        return

    prefix = _EXECUTE_QUERY_PREFIX if opts.execute else _QUERY_PREFIX
    query_string = prefix + " ".join(opts.query)

    emit_info("Thinking...", output_opts.output_format)

    host = _get_local_host(mng_ctx)
    backend = HeadlessClaudeBackend(host=host, mng_ctx=mng_ctx)
    system_prompt = _build_ask_context()
    chunks = backend.query(prompt=query_string, system_prompt=system_prompt)

    if opts.execute:
        # Accumulate all chunks for execute mode (don't stream to user)
        response = _accumulate_chunks(chunks)
        _execute_response(response=response, output_format=output_opts.output_format)
    else:
        _stream_or_accumulate_response(chunks=chunks, output_format=output_opts.output_format)


def _stream_or_accumulate_response(chunks: Iterator[str], output_format: OutputFormat) -> None:
    """Stream response chunks for HUMAN format, or accumulate for JSON/JSONL."""
    match output_format:
        case OutputFormat.HUMAN:
            for chunk in chunks:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            sys.stdout.write("\n")
            sys.stdout.flush()
        case OutputFormat.JSON:
            response = _accumulate_chunks(chunks)
            emit_final_json({"response": response})
        case OutputFormat.JSONL:
            response = _accumulate_chunks(chunks)
            emit_final_json({"event": "response", "response": response})
        case _ as unreachable:
            assert_never(unreachable)


def _execute_response(response: str, output_format: OutputFormat) -> None:
    """Execute the command from claude's response."""
    command = response.strip()
    if not command:
        raise MngError("claude returned an empty response; nothing to execute")

    try:
        args = shlex.split(command)
    except ValueError as err:
        raise MngError(f"claude returned a response that could not be parsed: {command}") from err
    if not args or args[0] != "mng":
        raise MngError(f"claude returned a response that is not a valid mng command: {command}")

    emit_info(f"Running: {command}", output_format)

    result = subprocess.run(args, capture_output=False)

    if result.returncode != 0:
        raise MngError(f"command failed (exit code {result.returncode}): {command}")


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="ask",
    one_line_description="Chat with mng for help [experimental]",
    synopsis="mng ask [--execute] QUERY...",
    description="""Ask a question and mng will generate the appropriate CLI command.
If no query is provided, shows general help about available commands
and common workflows.

When --execute is specified, the generated CLI command is executed
directly instead of being printed.""",
    examples=(
        ("Ask a question", 'mng ask "how do I create an agent?"'),
        ("Ask without quotes", "mng ask start a container with claude code"),
        ("Execute the generated command", "mng ask --execute forward port 8080 to the public internet"),
    ),
    see_also=(
        ("create", "Create an agent"),
        ("list", "List existing agents"),
        ("connect", "Connect to an agent"),
    ),
).register()

# Add pager-enabled help option to the ask command
add_pager_help_option(ask)
