import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Final
from typing import Iterator
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.address_params import AGENT_ADDRESS
from imbue.mngr.cli.address_params import HOST_ADDRESS
from imbue.mngr.cli.address_params import HOST_LOCATION_ADDRESS
from imbue.mngr.cli.agent_utils import find_agent_by_address_or_interactively
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import ConflictMode
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import SyncDirection
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.git_utils import find_git_worktree_root
from imbue.mngr_pair.api import TransferProgress
from imbue.mngr_pair.api import UnisonSyncer
from imbue.mngr_pair.api import pair_files


class PairCliOptions(CommonCliOptions):
    """Options passed from the CLI to the pair command."""

    source_pos: HostLocationAddress | None
    source: HostLocationAddress | None
    source_agent: AgentAddress | None
    source_host: HostAddress | None
    source_path: str | None
    target: str | None
    require_git: bool
    ignore_archives: bool
    links: bool
    start: bool
    sync_direction: str
    conflict: str
    uncommitted_changes: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]


def _emit_pair_started(
    source_path: Path,
    target_path: Path,
    output_opts: OutputOptions,
) -> None:
    """Emit a message when pairing starts."""
    data = {
        "source_path": str(source_path),
        "target_path": str(target_path),
    }
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("pair_started", data, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Pairing {} <-> {}", source_path, target_path)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_pair_syncing(output_opts: OutputOptions) -> None:
    """Emit a message once unison is up and the two replicas are actually being watched.

    Distinct from ``pair_started``, which is emitted before any work happens.
    Everything between the two can still fail (resolving a compatible unison on
    a remote host, the initial git reconciliation, the SSH handshake), so a
    programmatic caller needs this second event to tell "starting" from
    "syncing".
    """
    data: dict[str, str] = {}
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("pair_syncing", data, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Sync started. Press Ctrl+C to stop.")
        case _ as unreachable:
            assert_never(unreachable)


class PairActivityReporter(MutableModel):
    """Turns unison's transfer narration into ``pair_transferring`` events.

    A sync spends nearly all its life up but idle, so "running" and "moving
    bytes right now" are different facts and a consumer wants both. The edges
    always report; between them, unison's own progress numbers ride along at
    the interval ``UnisonSyncer`` throttles them to, so a consumer can say how
    far through a transfer is without measuring anything itself.
    """

    output_opts: OutputOptions = Field(frozen=True, description="Where and how to write the event")

    def on_transfer_change(self, is_transferring: bool, progress: TransferProgress | None) -> None:
        data: dict[str, object] = {"is_transferring": is_transferring}
        if progress is not None:
            data["bytes_done"] = progress.bytes_done
            data["bytes_total"] = progress.bytes_total
        match self.output_opts.output_format:
            case OutputFormat.JSON | OutputFormat.JSONL:
                emit_event("pair_transferring", data, self.output_opts.output_format)
            case OutputFormat.HUMAN:
                write_human_line(_transfer_human_line(is_transferring, progress))
            case _ as unreachable:
                assert_never(unreachable)


_BYTE_UNITS: Final[tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")


@pure
def format_byte_size(byte_count: int) -> str:
    """``byte_count`` for a person to read, in the units unison measured it in.

    Powers of 1024 under decimal names, which is what a file manager shows and
    what the number came from; one decimal place above bytes, because a
    transfer that reads "1.4 GB" is more use than one that reads "1 GB".
    """
    size = float(byte_count)
    for unit in _BYTE_UNITS:
        if abs(size) < 1024.0 or unit == _BYTE_UNITS[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    raise AssertionError("the loop above returns on its last unit")


def _transfer_human_line(is_transferring: bool, progress: TransferProgress | None) -> str:
    """What a terminal is told about a transfer, with unison's numbers when it gave any."""
    if not is_transferring:
        return "Up to date."
    if progress is None:
        return "Transferring..."
    return "Transferring... ({} of {})".format(
        format_byte_size(progress.bytes_done), format_byte_size(progress.bytes_total)
    )


def _emit_pair_stopped(output_opts: OutputOptions) -> None:
    """Emit a message when pairing stops."""
    data: dict[str, str] = {}
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("pair_stopped", data, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Pairing stopped")
        case _ as unreachable:
            assert_never(unreachable)


class SyncStopSignal(MutableModel):
    """What the command waits on while a sync runs: unison ending, or a SIGTERM.

    The obvious implementation -- block in ``UnisonSyncer.wait`` and have the
    SIGTERM handler raise ``KeyboardInterrupt`` -- is broken, and quietly. That
    wait blocks in ``Thread.join``, and CPython's bpo-45274 handling marks a
    joined thread *stopped* when a signal handler raises out of the join, even
    though the thread is still running. The teardown that follows then reads
    the unison-watching thread as already finished and never signals unison, so
    the command exits and leaves unison syncing the user's folders behind it.

    So nothing is ever raised into that join. A thread does the waiting, the
    signal handler only sets a flag, and the command waits on this event --
    leaving the join, and therefore the teardown, intact.
    """

    model_config = {"arbitrary_types_allowed": True}

    syncer: UnisonSyncer = Field(frozen=True, description="The sync being waited on")
    is_stop_requested: bool = Field(default=False, description="A signal asked for the sync to stop")
    exit_code: int | None = Field(default=None, description="Unison's exit code, once it has ended on its own")
    _event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def on_signal(self, signal_number: int, frame: FrameType | None) -> None:
        """Signal handler: record the request and wake the command. Raises nothing."""
        self.is_stop_requested = True
        self._event.set()

    def wait_for_syncer(self) -> None:
        """Thread body: wait for unison to end on its own, then wake the command."""
        try:
            self.exit_code = self.syncer.wait()
        except ConcurrencyGroupError as e:
            logger.warning("Could not wait on the unison process: {}", e)
            self.exit_code = 1
        finally:
            self._event.set()

    def wait(self) -> None:
        """Block until unison ends or a signal asks for the sync to stop."""
        self._event.wait()


@contextmanager
def _sigterm_requests_stop(stop_signal: SyncStopSignal) -> Iterator[None]:
    """Make SIGTERM ask the sync to stop, the way Ctrl+C does.

    Without this, SIGTERM kills the command outright: the teardown never runs
    and unison is left behind still syncing, with nothing watching it. A
    program that starts a pairing stops it that way, having no terminal to
    type Ctrl+C at.
    """
    previous_handler = signal.signal(signal.SIGTERM, stop_signal.on_signal)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _resolve_started_host(
    host_address: HostAddress, is_start_desired: bool, mngr_ctx: MngrContext
) -> OnlineHostInterface:
    """Bring the host named by ``host_address`` online, naming no agent on it.

    Raises :class:`UserInputError` when the address matches no host or more than
    one, and when the host is offline and starting it was not asked for.
    """
    outcome = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    host_ref = filter_one_host(host_address, list(outcome.agents_by_host.keys()))
    provider = get_provider_instance(host_ref.provider_name, mngr_ctx)
    online_host, _was_started = ensure_host_started(
        provider.get_host(host_ref.host_id), is_start_desired=is_start_desired, provider=provider
    )
    return online_host


def _resolve_source(
    source_address: AgentAddress | None,
    source_subpath: Path | None,
    opts: PairCliOptions,
    mngr_ctx: MngrContext,
) -> tuple[AgentInterface | None, OnlineHostInterface, Path]:
    """Resolve what is being paired with, and the absolute path on its side.

    Naming only a host pairs with the host itself: no agent is resolved, none
    has to exist, and ``--source-path`` must therefore be absolute, since there
    is no agent work directory for a relative one to be relative to. Naming an
    agent (or naming nothing, and picking one) keeps that agent's work directory
    as the default source.
    """
    if source_address is None and opts.source_host is not None:
        if source_subpath is None or not source_subpath.is_absolute():
            raise UserInputError(
                "Pairing with a host rather than an agent needs an absolute --source-path, "
                "because there is no agent work directory to resolve a relative one against."
            )
        if opts.require_git:
            raise UserInputError(
                "Git sync needs an agent to sync with; pass --no-require-git to pair with a host, "
                "or name an agent instead of only a host."
            )
        return None, _resolve_started_host(opts.source_host, opts.start, mngr_ctx), source_subpath

    host_ref, agent_ref = find_agent_by_address_or_interactively(
        mngr_ctx=mngr_ctx,
        address=source_address,
        host_filter=opts.source_host,
    )
    agent, host = resolve_to_started_host_and_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=opts.start,
        mngr_ctx=mngr_ctx,
    )
    if source_subpath is None:
        return agent, host, agent.work_dir
    if source_subpath.is_absolute():
        return agent, host, source_subpath
    return agent, host, agent.work_dir / source_subpath


@click.command()
@click.argument("source_pos", type=HOST_LOCATION_ADDRESS, default=None, required=False, metavar="SOURCE")
@optgroup.group("Source Selection")
@optgroup.option(
    "--source",
    "source",
    type=HOST_LOCATION_ADDRESS,
    help="Source specification: AGENT[@HOST[.PROVIDER]][:PATH]",
)
@optgroup.option("--source-agent", type=AGENT_ADDRESS, help="Source agent address (NAME[@HOST[.PROVIDER]])")
@optgroup.option("--source-host", type=HOST_ADDRESS, help="Source host address (HOST[.PROVIDER])")
@optgroup.option(
    "--source-path",
    help="Path within the agent's work directory, or an absolute path when pairing with a host",
)
@optgroup.group("Target")
@optgroup.option(
    "--target",
    "target",
    type=click.Path(),
    help="Local target directory [default: nearest git root or current directory]",
)
@optgroup.group("General")
@optgroup.option(
    "--links/--no-links",
    "links",
    default=True,
    show_default=True,
    help=(
        "Carry symbolic links inside the directories across. Turn off when the two sides are "
        "different machines, where a link's target need not mean the same thing on both."
    ),
)
@optgroup.option(
    "--ignore-archives/--no-ignore-archives",
    "ignore_archives",
    default=False,
    show_default=True,
    help=(
        "Pair as though these two paths had never been paired before. For a caller that just "
        "created one of the directories, where any archive from a previous pairing describes "
        "a directory that no longer exists."
    ),
)
@optgroup.option(
    "--start/--no-start",
    "start",
    default=True,
    show_default=True,
    help="Automatically start the host if offline",
)
@optgroup.group("Git Handling")
@optgroup.option(
    "--require-git/--no-require-git",
    default=True,
    help="Require that both source and target are git repositories [default: require git]",
)
@optgroup.option(
    "--uncommitted-changes",
    type=click.Choice(["stash", "clobber", "merge", "fail"], case_sensitive=False),
    default="fail",
    show_default=True,
    help="How to handle uncommitted changes during initial git sync. The initial sync aborts immediately if unresolved conflicts exist, regardless of this setting.",
)
@optgroup.group("Sync Behavior")
@optgroup.option(
    "--sync-direction",
    type=click.Choice(["both", "forward", "reverse"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Sync direction: both (bidirectional), forward (source->target), reverse (target->source)",
)
@optgroup.option(
    "--conflict",
    type=click.Choice(["newer", "source", "target", "ask"], case_sensitive=False),
    default="newer",
    show_default=True,
    help="Conflict resolution mode (only matters for bidirectional sync). 'newer' prefers the file with the more recent modification time (uses unison's -prefer newer; note that clock skew between machines can cause incorrect results). 'source' and 'target' always prefer that side. 'ask' prompts interactively [future].",
)
@optgroup.group("File Filtering")
@optgroup.option(
    "--include",
    multiple=True,
    help="Include files matching glob pattern [repeatable]",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude files matching glob pattern [repeatable]",
)
@add_common_options
@click.pass_context
def pair(ctx: click.Context, **kwargs) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="pair",
        command_class=PairCliOptions,
    )

    # Merge positional and named arguments (named option takes precedence)
    effective_source_loc: HostLocationAddress | None = opts.source if opts.source is not None else opts.source_pos

    # Build source agent address and sub-path
    source_address: AgentAddress | None = None
    source_subpath: Path | None = None
    if effective_source_loc is not None:
        if effective_source_loc.agent is not None:
            source_address = AgentAddress(agent=effective_source_loc.agent, host=effective_source_loc.host)
        source_subpath = effective_source_loc.path
    if opts.source_agent is not None:
        if source_address is not None and source_address != opts.source_agent:
            raise UserInputError("Cannot specify both --source and --source-agent with different values")
        source_address = opts.source_agent
    if opts.source_path is not None:
        explicit_source_path = Path(opts.source_path)
        if source_subpath is not None and source_subpath != explicit_source_path:
            raise UserInputError("Cannot specify both a subpath in source and --source-path")
        source_subpath = explicit_source_path

    # Determine target path
    if opts.target is not None:
        target_path = Path(opts.target)
    else:
        # Default to nearest git root, or current directory
        git_root = find_git_worktree_root(None, mngr_ctx.concurrency_group)
        target_path = git_root if git_root is not None else Path.cwd()

    # Find the agent, or the host on its own when only a host was named. A sync
    # is between two directories on two machines, and the agent is consulted for
    # nothing but git state -- so naming one is required only when git state is
    # in play, and pairing with a whole host needs no agent to exist at all.
    agent, host, source_path = _resolve_source(
        source_address=source_address,
        source_subpath=source_subpath,
        opts=opts,
        mngr_ctx=mngr_ctx,
    )

    if agent is None:
        emit_info(f"Pairing with host: {host.get_name()}", output_opts.output_format)
    else:
        emit_info(f"Pairing with agent: {agent.name}", output_opts.output_format)

    # Parse enum options
    sync_direction = SyncDirection(opts.sync_direction.upper())
    conflict_mode = ConflictMode(opts.conflict.upper())
    uncommitted_changes_mode = UncommittedChangesMode(opts.uncommitted_changes.upper())

    _emit_pair_started(source_path, target_path, output_opts)

    # Start the pair sync
    try:
        with pair_files(
            agent=agent,
            host=host,
            agent_path=source_path,
            local_path=target_path,
            sync_direction=sync_direction,
            conflict_mode=conflict_mode,
            is_require_git=opts.require_git,
            uncommitted_changes=uncommitted_changes_mode,
            exclude_patterns=opts.exclude,
            include_patterns=opts.include,
            cg=mngr_ctx.concurrency_group,
            is_ignoring_archives=opts.ignore_archives,
            is_syncing_links=opts.links,
            on_transfer_change=PairActivityReporter(output_opts=output_opts).on_transfer_change,
        ) as syncer:
            stop_signal = SyncStopSignal(syncer=syncer)
            mngr_ctx.concurrency_group.start_new_thread(
                target=stop_signal.wait_for_syncer,
                name="mngr-pair-syncer-waiter",
                daemon=True,
                is_checked=False,
            )
            _emit_pair_syncing(output_opts)

            with _sigterm_requests_stop(stop_signal):
                stop_signal.wait()

            # A nonzero code after a stop was asked for is just how the process
            # died on the way out, not a failure worth reporting.
            if not stop_signal.is_stop_requested and stop_signal.exit_code not in (None, 0):
                raise MngrError(f"Unison exited with code {stop_signal.exit_code}")
    except KeyboardInterrupt:
        logger.debug("Received keyboard interrupt")
    finally:
        _emit_pair_stopped(output_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="pair",
    one_line_description="Continuously sync files between an agent and local directory [experimental]",
    synopsis="mngr pair [SOURCE] [--source <SOURCE>] [--target <DIR>] [--sync-direction <DIR>] [--conflict <MODE>] [--include PATTERN] [--exclude PATTERN] [--[no-]start] [--[no-]ignore-archives] [--[no-]links]",
    description="""This command establishes a bidirectional file sync between an agent's working
directory and a local directory. Changes are watched and synced in real-time.

If git repositories exist on both sides, the command first synchronizes git
state (branches and commits) before starting the continuous file sync.

Remote agents are supported. unison is a client/server protocol, so pairing
with a remote agent runs a second unison on the host over mngr's own SSH
transport. mngr uses whatever usable unison is already installed there, and
otherwise installs a pinned static build into ~/.mngr/bin on the host. Both
ends need unison 2.52 or newer -- older versions cannot interoperate at all --
along with the unison-fsmonitor helper that unison watches for changes
through. The unison packaged by Debian and Ubuntu has no such helper, so it
does not count as usable. mngr only installs that build for Linux x86_64
(upstream publishes no Linux arm64 binary at all), so on any other platform
both binaries have to be installed by hand.

Press Ctrl+C to stop the sync.

During rapid concurrent edits, changes will be debounced to avoid partial writes [future].""",
    examples=(
        ("Pair with an agent", "mngr pair my-agent"),
        ("Pair to specific local directory", "mngr pair my-agent --target ./local-dir"),
        ("One-way sync (source to target)", "mngr pair my-agent --sync-direction=forward"),
        ("Prefer source on conflicts", "mngr pair my-agent --conflict=source"),
        ("Filter to specific host", "mngr pair my-agent --source-host localhost"),
        ("Pair with an agent on a remote host", "mngr pair my-agent@my-vps"),
        ("Use --source-agent flag", "mngr pair --source-agent my-agent --target ./local-copy"),
    ),
    see_also=(
        ("rsync", "One-shot file sync between local and a remote host or agent"),
        ("git", "Push or pull git commits between local and a remote agent or host"),
        ("create", "Create a new agent"),
        ("list", "List agents to find one to pair with"),
    ),
).register()

add_pager_help_option(pair)
