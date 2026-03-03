import json
import shlex
import subprocess
import sys
import tempfile
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.help_formatter import get_all_help_metadata
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.errors import MngError
from imbue.mng.primitives import OutputFormat

_QUERY_PREFIX: Final[str] = (
    "answer this question about `mng`. "
    "respond concisely with the mng command(s) and a brief explanation. "
    "no markdown formatting. "
    "here are some example questions and ideal responses:\n\n"
    #
    "user: How do I create a container on modal with custom packages installed by default?\n"
    "response: Simply run:\n"
    '    mng create --in modal --build-arg "--file path/to/Dockerfile"\n'
    "If you don't have a Dockerfile for your project, run:\n"
    "    mng bootstrap\n"
    "from the repo where you would like a Dockerfile created.\n\n"
    #
    "user: How do I spin up 5 agents on the cloud?\n"
    "response: mng create -n 5 --in modal\n\n"
    #
    "user: How do I run multiple agents on the same cloud machine to save costs?\n"
    "response: Create them on a shared host:\n"
    "    mng create agent-1 --in modal --host shared-host\n"
    "    mng create agent-2 --in modal --host shared-host\n\n"
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
    "user: How do I create an agent with environment secrets and GitHub SSH access?\n"
    "response: mng create --env-file .env.secrets --known-host github.com\n\n"
    #
    "user: How do I create an agent from a saved template?\n"
    "response: mng create --template gpu-heavy\n\n"
    #
    "user: How do I run a test watcher alongside my agent?\n"
    "response: Use --add-command to open an extra tmux window:\n"
    '    mng create --add-command "watch -n5 pytest"\n\n'
    #
    "user: How do I get a list of running agent names as JSON?\n"
    "response: mng list --running --format json\n\n"
    #
    "user: How do I watch agent status in real time?\n"
    "response: mng list --watch 5\n\n"
    #
    "user: How do I message only agents with a specific tag?\n"
    "response: Use a CEL filter:\n"
    '    mng message --include \'tags.feature == "auth"\' -m "run the auth test suite"\n\n'
    #
    "user: How do I launch 3 independent tasks in parallel on the cloud?\n"
    "response: Run multiple creates with --no-connect:\n"
    '    mng create --in modal --no-connect -m "implement dark mode"\n'
    '    mng create --in modal --no-connect -m "add i18n support"\n'
    '    mng create --in modal --no-connect -m "optimize database queries"\n\n'
    #
    "user: How do I launch an agent on Modal?\n"
    "response: mng create --in modal\n\n"
    #
    "user: How do I launch an agent locally?\n"
    "response: mng create --in local\n\n"
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
    "response: mng create -n 5 --in modal\n\n"
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
    'response: mng create --in modal --no-connect -m "fix all failing tests and commit"\n\n'
    #
    "user: list running agents as json\n"
    "response: mng list --running --format json\n\n"
    #
    "user: clone my-agent into a new agent called experiment\n"
    "response: mng clone my-agent experiment\n\n"
    #
    "user: pull git commits from my-agent\n"
    "response: mng pull my-agent --sync-mode git\n\n"
    #
    "user: create a local agent with opus\n"
    "response: mng create --in local -- --model opus\n\n"
    #
    "now respond with ONLY the mng command for this request:\n"
    "user: "
)

_PROCESS_WAIT_TIMEOUT_SECONDS: Final[int] = 10


class ClaudeBackendInterface(MutableModel, ABC):
    """Abstraction over the claude subprocess for testability."""

    @abstractmethod
    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        """Send a prompt to claude and yield response text chunks."""


@pure
def _extract_text_delta(line: str) -> str | None:
    """Extract text from a stream-json content_block_delta event.

    Returns the delta text if the line is a content_block_delta with a text_delta,
    or None otherwise.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if parsed.get("type") != "stream_event":
        return None

    event = parsed.get("event")
    if not isinstance(event, dict):
        return None

    if event.get("type") != "content_block_delta":
        return None

    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None

    if delta.get("type") != "text_delta":
        return None

    text = delta.get("text")
    if isinstance(text, str):
        return text

    return None


class SubprocessClaudeBackend(ClaudeBackendInterface):
    """Runs claude in a subprocess from an empty temp directory with streaming."""

    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        with tempfile.TemporaryDirectory(prefix="mng-ask-") as tmp_dir:
            try:
                process = subprocess.Popen(
                    [
                        "claude",
                        "--print",
                        "--system-prompt",
                        system_prompt,
                        "--output-format",
                        "stream-json",
                        "--verbose",
                        "--include-partial-messages",
                        "--tools",
                        "",
                        "--no-session-persistence",
                        prompt,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    cwd=tmp_dir,
                )
            except FileNotFoundError:
                raise MngError(
                    "claude is not installed or not found in PATH. "
                    "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code/overview"
                ) from None

            assert process.stdout is not None

            try:
                # Stream text deltas from stdout
                is_error = False
                for line in process.stdout:
                    stripped = line.strip()
                    if not stripped:
                        continue

                    # Check for result events that indicate completion status
                    try:
                        parsed = json.loads(stripped)
                        if parsed.get("type") == "result" and parsed.get("is_error"):
                            is_error = True
                    except (json.JSONDecodeError, ValueError):
                        pass

                    text = _extract_text_delta(stripped)
                    if text is not None:
                        yield text

                # Wait for process to finish and check exit code
                process.wait(timeout=_PROCESS_WAIT_TIMEOUT_SECONDS)

                if is_error or process.returncode != 0:
                    assert process.stderr is not None
                    stderr_content = process.stderr.read().strip()
                    detail = stderr_content or "unknown error (no output captured)"
                    raise MngError(f"claude failed (exit code {process.returncode}): {detail}")
            finally:
                # Ensure the subprocess is cleaned up if the generator exits early
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=_PROCESS_WAIT_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


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
    _mng_ctx, output_opts, opts = setup_command_context(
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

    backend = SubprocessClaudeBackend()
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
