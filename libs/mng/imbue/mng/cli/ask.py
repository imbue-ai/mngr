import shlex
import subprocess
import sys
import tempfile
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
    '    mng create --in modal -b "--file path/to/Dockerfile"\n'
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
    "response: mng create --in local -- --model opus\n\n"
    #
    "now respond with ONLY the mng command for this request:\n"
    "user: "
)


_MNG_REPO_URL: Final[str] = "https://github.com/imbue-ai/mng"


# Monorepo library directory names whose source the ask agent can read.
# Keep in sync with scripts/utils.py PACKAGES -- validated by
# test_monorepo_package_dirs_matches_release_packages in ask_test.py.
_MONOREPO_PACKAGE_DIRS: Final[tuple[str, ...]] = (
    "concurrency_group",
    "imbue_common",
    "mng",
    "mng_kanpan",
    "mng_opencode",
    "mng_pair",
    "mng_tutor",
)


def _find_source_directories() -> list[Path]:
    """Find source directories for mng and all related monorepo packages.

    For a source checkout (editable install / uv run), returns the project root
    of every library listed in ``_MONOREPO_PACKAGE_DIRS`` that exists on disk.
    Detected by the mng project root containing a pyproject.toml.

    For a regular install (site-packages), all packages merge into a single
    ``imbue/`` directory, so a single path is returned.

    Returns an empty list only if the directory structure is completely
    unexpected.
    """
    # parents[0]=cli/, [1]=mng/, [2]=imbue/, [3]=project-root-or-site-packages
    imbue_dir = Path(__file__).resolve().parents[2]
    project_root = imbue_dir.parent

    # Source checkout: mng's project root has pyproject.toml.
    if (project_root / "pyproject.toml").is_file():
        return _find_source_checkout_directories(project_root)

    # Installed package: scope to imbue/ (covers mng + plugins + deps)
    if (imbue_dir / "mng").is_dir():
        return [imbue_dir]

    return []


def _find_source_checkout_directories(mng_project_root: Path) -> list[Path]:
    """Return project roots for all monorepo libraries listed in _MONOREPO_PACKAGE_DIRS."""
    libs_dir = mng_project_root.parent
    directories: list[Path] = []
    for dir_name in _MONOREPO_PACKAGE_DIRS:
        candidate = libs_dir / dir_name
        if candidate.is_dir():
            directories.append(candidate)
    return sorted(directories)


def _find_mng_package_dir(source_directories: list[Path]) -> Path | None:
    """Locate the mng Python package directory among the source directories."""
    for d in source_directories:
        # Source checkout: project root contains imbue/mng/
        if (d / "imbue" / "mng").is_dir():
            return d / "imbue" / "mng"
        # Installed: imbue/ dir contains mng/
        if (d / "mng").is_dir() and (d / "mng" / "cli").is_dir():
            return d / "mng"
    return None


def _build_source_access_context(source_directories: list[Path]) -> str:
    """Build system prompt section describing available source code access."""
    parts = [
        "\n\n# Source Code Access\n\n",
        "You can use the Read, Glob, and Grep tools to explore source code "
        "when answering questions.\n\n",
    ]

    mng_pkg = _find_mng_package_dir(source_directories)
    mng_root: Path | None = None

    if mng_pkg is not None:
        # The mng project root is two levels above imbue/mng/, or one above mng/.
        mng_root = mng_pkg.parent.parent if (mng_pkg.parent.name == "imbue") else mng_pkg.parent
        parts.append("Key mng directories:\n")
        if (mng_root / "docs").is_dir():
            parts.append(f"- {mng_root}/docs/ - User-facing documentation (markdown)\n")
        parts += [
            f"- {mng_pkg}/ - Python source code\n",
            f"- {mng_pkg}/cli/ - CLI command implementations\n",
            f"- {mng_pkg}/agents/ - Agent type implementations\n",
            f"- {mng_pkg}/providers/ - Provider backends (docker, modal, local)\n",
            f"- {mng_pkg}/plugins/ - Plugin system\n",
            f"- {mng_pkg}/config/ - Configuration handling\n",
        ]

    other_dirs = [d for d in source_directories if d != mng_root]
    if other_dirs:
        parts.append("\nOther accessible source directories (plugins and dependencies):\n")
        for d in other_dirs:
            parts.append(f"- {d}/\n")

    return "".join(parts)


def _build_web_access_context() -> str:
    """Build system prompt section describing available web access."""
    return (
        "\n\n# Web Access\n\n"
        "You have access to the WebFetch tool, restricted to the mng GitHub repository.\n"
        f"The repository is at: {_MNG_REPO_URL}\n"
        "Use WebFetch to read source files, documentation, issues, or pull requests\n"
        "from the repository when the information is not available locally.\n"
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


def _build_read_only_tools_and_permissions(
    source_directories: list[Path],
    *,
    allow_web: bool,
) -> tuple[str, tuple[str, ...]]:
    """Build the --tools value and --allowedTools args for the ask agent.

    When source_directories is non-empty, Read/Glob/Grep are included and
    path-scoped to those directories. When empty, only WebFetch (if enabled)
    is available.

    Returns (tools_csv, allowed_tools_args).
    """
    tools_parts: list[str] = []
    allowed_tools_args: tuple[str, ...] = ()

    if source_directories:
        tools_parts.append("Read,Glob,Grep")
        for source_directory in source_directories:
            scope = f"//{source_directory}/**"
            allowed_tools_args += (
                "--allowedTools",
                f"Read({scope})",
                "--allowedTools",
                f"Glob({scope})",
                "--allowedTools",
                f"Grep({scope})",
            )

    if allow_web:
        tools_parts.append("WebFetch")
        allowed_tools_args += (
            "--allowedTools",
            "WebFetch(domain:github.com)",
            "--allowedTools",
            "WebFetch(domain:raw.githubusercontent.com)",
        )

    tools_csv = ",".join(tools_parts)
    return tools_csv, allowed_tools_args


@contextmanager
def _headless_claude_output(
    mng_ctx: MngContext,
    prompt: str,
    system_prompt: str,
    *,
    source_directories: list[Path] | None = None,
    allow_web: bool = False,
) -> Iterator[StreamingHeadlessAgentMixin]:
    """Create a HeadlessClaude agent, yield it, and destroy it on exit.

    Creates a temporary directory as the work path (no git branch creation),
    and passes claude args for headless operation (--system-prompt, --output-format
    stream-json, --tools, --no-session-persistence).

    When source_directories is non-empty, Read/Glob/Grep tools are path-scoped
    to those directories. When allow_web is True, WebFetch is restricted to
    GitHub domains.
    """
    provider = get_provider_instance(LOCAL_PROVIDER_NAME, mng_ctx)
    host_interface = provider.get_host(HostName("localhost"))
    if not isinstance(host_interface, OnlineHostInterface):
        raise MngError("Local host is not online")
    host = host_interface

    with tempfile.TemporaryDirectory(prefix="mng-ask-") as tmp_dir:
        # Write prompt and system prompt to files in the work dir so the
        # shell command can read them via $(cat ...).  Passing them inline
        # as agent_args would exceed tmux's command length limit.
        work_path = Path(tmp_dir)
        (work_path / ".mng-system-prompt").write_text(system_prompt)
        (work_path / ".mng-prompt").write_text(prompt)

        tools, allowed_tools_args = _build_read_only_tools_and_permissions(
            source_directories or [], allow_web=allow_web,
        )

        agent_args = (
            "--system-prompt",
            '"$(cat "$MNG_AGENT_WORK_DIR/.mng-system-prompt")"',
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            f'"{tools}"',
            *allowed_tools_args,
            "--permission-mode",
            "dontAsk",
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


class HeadlessClaudeBackend(ClaudeBackendInterface):
    """Runs claude via a HeadlessClaude agent for proper agent lifecycle management."""

    mng_ctx: MngContext
    source_directories: list[Path] = []
    allow_web: bool = False

    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        with _headless_claude_output(
            self.mng_ctx,
            prompt,
            system_prompt,
            source_directories=self.source_directories,
            allow_web=self.allow_web,
        ) as agent:
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
    allow_web: bool


@click.command(name="ask")
@click.argument("query", nargs=-1, required=False)
@optgroup.group("Behavior")
@optgroup.option(
    "--execute",
    is_flag=True,
    help="Execute the generated CLI command instead of just printing it",
)
@optgroup.option(
    "--allow-web",
    is_flag=True,
    help="Allow fetching content from the mng GitHub repository",
)
@add_common_options
@click.pass_context
def ask(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _ask_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _run_ask_query(
    backend: ClaudeBackendInterface,
    query_string: str,
    *,
    source_directories: list[Path],
    execute: bool,
    allow_web: bool,
    output_format: OutputFormat,
) -> None:
    """Run a query against the claude backend and handle the response."""
    system_prompt = _build_ask_context()
    if source_directories:
        system_prompt += _build_source_access_context(source_directories)
    if allow_web:
        system_prompt += _build_web_access_context()
    chunks = backend.query(prompt=query_string, system_prompt=system_prompt)

    if execute:
        response = _accumulate_chunks(chunks)
        _execute_response(response=response, output_format=output_format)
    else:
        _stream_or_accumulate_response(chunks=chunks, output_format=output_format)


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

    source_dirs = _find_source_directories()
    backend = HeadlessClaudeBackend(
        mng_ctx=mng_ctx, source_directories=source_dirs, allow_web=opts.allow_web,
    )
    _run_ask_query(
        backend=backend,
        query_string=query_string,
        source_directories=source_dirs,
        execute=opts.execute,
        allow_web=opts.allow_web,
        output_format=output_opts.output_format,
    )


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
    synopsis="mng ask [--execute] [--allow-web] QUERY...",
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
