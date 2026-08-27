import platform
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from typing import Iterator
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.git import GitContextInterface
from imbue.mngr.api.git import LocalGitContext
from imbue.mngr.api.git import RemoteGitContext
from imbue.mngr.api.git import git_fetch
from imbue.mngr.api.git import git_pull
from imbue.mngr.api.git import git_push
from imbue.mngr.api.git import stash_guard
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ConflictMode
from imbue.mngr.primitives import SyncDirection
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.deps import SystemDependency
from imbue.mngr.utils.git_utils import is_ancestor
from imbue.mngr_pair.remote import SshEndpoint
from imbue.mngr_pair.remote import UnisonRoot
from imbue.mngr_pair.remote import check_local_unison_version
from imbue.mngr_pair.remote import ensure_remote_unison
from imbue.mngr_pair.remote import write_ssh_wrapper_script

_GIT_FETCH_TIMEOUT_SECONDS: Final[float] = 30.0


class GitSyncAction(FrozenModel):
    """Describes which side (agent or local) has commits the other doesn't."""

    agent_is_ahead: bool = Field(
        default=False,
        description="True if agent has commits that local doesn't have",
    )
    local_is_ahead: bool = Field(
        default=False,
        description="True if local has commits that agent doesn't have",
    )
    agent_branch: str = Field(
        description="The branch name on the agent side",
    )
    local_branch: str = Field(
        description="The branch name on the local side",
    )


class UnisonSyncer(MutableModel):
    """Manages a unison process for continuous bidirectional file synchronization."""

    cg: ConcurrencyGroup = Field(frozen=True, description="Concurrency group for managing the unison process")
    source_root: UnisonRoot = Field(frozen=True, description="Source replica to sync from (local path or ssh:// root)")
    target_root: UnisonRoot = Field(frozen=True, description="Target replica to sync to (local path or ssh:// root)")
    ssh_wrapper_path: Path | None = Field(
        frozen=True,
        default=None,
        description="Executable ssh wrapper passed as -sshcmd; None when both replicas are local",
    )
    remote_unison_path: Path | None = Field(
        frozen=True,
        default=None,
        description="Path to unison on the remote host, passed as -servercmd; None when both replicas are local",
    )
    sync_direction: SyncDirection = Field(
        frozen=True,
        default=SyncDirection.BOTH,
        description="Direction of sync: forward, reverse, or both",
    )
    conflict_mode: ConflictMode = Field(
        frozen=True,
        default=ConflictMode.NEWER,
        description="How to resolve conflicts",
    )
    exclude_patterns: tuple[str, ...] = Field(
        frozen=True,
        default=(),
        description="Glob patterns to exclude from sync",
    )
    include_patterns: tuple[str, ...] = Field(
        frozen=True,
        default=(),
        description="Glob patterns to include in sync",
    )
    _running_process: RunningProcess | None = PrivateAttr(default=None)
    _started_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    model_config = {"arbitrary_types_allowed": True}

    def _build_unison_command(self) -> list[str]:
        """Build the unison command line arguments."""
        source_arg = self.source_root.as_root_arg()
        target_arg = self.target_root.as_root_arg()
        cmd = [
            "unison",
            source_arg,
            target_arg,
            "-repeat",
            "watch",
            "-auto",
            "-batch",
            "-ignore",
            "Name .git",
        ]

        # Reaching a remote replica needs both: -sshcmd decides how the connection is
        # made (the ssh:// root syntax has nowhere to put a port or a key), and
        # -servercmd decides which unison answers, so a too-old one on the host's PATH
        # can never be picked up by accident.
        if self.ssh_wrapper_path is not None:
            cmd.extend(["-sshcmd", str(self.ssh_wrapper_path)])
        if self.remote_unison_path is not None:
            cmd.extend(["-servercmd", str(self.remote_unison_path)])

        # Add conflict preference based on mode
        match self.conflict_mode:
            case ConflictMode.SOURCE:
                cmd.extend(["-prefer", source_arg])
            case ConflictMode.TARGET:
                cmd.extend(["-prefer", target_arg])
            case ConflictMode.NEWER:
                cmd.extend(["-prefer", "newer"])
            case ConflictMode.ASK:
                raise NotImplementedError("ConflictMode.ASK is not yet implemented")
            case _ as unreachable:
                assert_never(unreachable)

        # Add sync direction constraints
        if self.sync_direction == SyncDirection.FORWARD:
            cmd.extend(["-force", source_arg])
        elif self.sync_direction == SyncDirection.REVERSE:
            cmd.extend(["-force", target_arg])
        else:
            # SyncDirection.BOTH - bidirectional sync, no force flag needed
            pass

        # Add exclude patterns
        for pattern in self.exclude_patterns:
            cmd.extend(["-ignore", f"Name {pattern}"])

        # Add include patterns
        for pattern in self.include_patterns:
            cmd.extend(["-path", pattern])

        return cmd

    def _on_output(self, line: str, is_stdout: bool) -> None:
        """Handle a line of output from the unison process.

        Sets the _started_event on first output, which signals that unison has
        actually initialized (not just that the OS process was spawned).
        """
        logger.debug("unison: {}", line.rstrip())
        self._started_event.set()

    def start(self) -> None:
        """Start the unison sync process."""
        self._started_event.clear()
        cmd = self._build_unison_command()
        logger.debug("Starting unison with command: {}", " ".join(cmd))

        self._running_process = self.cg.run_process_in_background(
            cmd,
            on_output=self._on_output,
            is_checked_by_group=False,
            # Unison runs in continuous-sync mode for the whole pairing session and
            # narrates every transfer, so retaining its output would grow without
            # bound. ``_on_output`` already logs each line as it arrives, and nothing
            # reads the output back.
            is_output_accumulated=False,
        )

        logger.info(
            "Started continuous sync between {} and {}",
            self.source_root.as_root_arg(),
            self.target_root.as_root_arg(),
        )

    def stop(self) -> None:
        """Stop the unison sync process gracefully, or do nothing if none is running."""
        if self._running_process is None:
            return
        logger.debug("Stopping unison process")
        self._running_process.terminate()
        self._running_process = None
        logger.info("Stopped continuous sync")

    def wait(self) -> int:
        """Wait for the unison process to complete and return the exit code."""
        if self._running_process is None:
            return 0
        return self._running_process.wait()

    @property
    def is_running(self) -> bool:
        """Check if the unison process is currently running.

        Returns True only when the OS process is alive AND unison has produced
        at least one line of output (meaning it has actually initialized, not
        just that the process was spawned).
        """
        if self._running_process is None:
            return False
        if self._running_process.is_finished():
            return False
        return self._started_event.is_set()

    def wait_for_started(self, timeout: float) -> None:
        """Block until unison has produced its first output line, or until timeout elapses."""
        self._started_event.wait(timeout=timeout)


_UNISON = SystemDependency(
    binary="unison",
    purpose="pair mode",
    macos_hint="brew install unison",
    linux_hint=(
        "Take unison from a release at https://github.com/bcpierce00/unison/releases. "
        "The Debian and Ubuntu package is not enough: it ships no unison-fsmonitor, and "
        "Ubuntu 22.04's unison is 2.51, which cannot talk to a newer one."
    ),
)
_UNISON_FSMONITOR = SystemDependency(
    binary="unison-fsmonitor",
    purpose="pair mode (the helper unison watches for changes through)",
    macos_hint="brew install autozimu/formulas/unison-fsmonitor",
    linux_hint=(
        "Take unison-fsmonitor from a release at https://github.com/bcpierce00/unison/releases. "
        "No Debian or Ubuntu package provides it."
    ),
)


def require_unison() -> None:
    """Require unison, plus unison-fsmonitor on macOS.

    Continuous sync watches each replica through a unison-fsmonitor helper, on every
    platform -- unison has no built-in watcher anywhere. Only the macOS requirement is
    enforced here, so a Linux machine missing the helper still gets as far as unison's
    own "No file monitoring helper program found".
    """
    _UNISON.require()
    if platform.system() == "Darwin":
        _UNISON_FSMONITOR.require()


def git_context_for_host(host: OnlineHostInterface, cg: ConcurrencyGroup) -> GitContextInterface:
    """Git context for the agent side: local subprocesses for a local host, SSH otherwise."""
    if host.is_local:
        return LocalGitContext(cg=cg)
    return RemoteGitContext(host=host)


def determine_git_sync_actions(
    agent_path: Path,
    local_path: Path,
    host: OnlineHostInterface,
    cg: ConcurrencyGroup,
) -> GitSyncAction | None:
    """Determine what git sync actions are needed between agent and local repos.

    Returns None if either side is not a git repository.

    Comparing ancestry needs both sides' commits reachable from one repository.
    mngr fetches the agent's branch *into the local repo*, over the same URL and
    SSH transport ``git pull`` uses. The other direction is not an option: the
    agent's repo may be on another machine, where a local filesystem path names
    nothing (or worse, names a different repository). Fetching this way also puts
    the write on the side the user owns rather than in the agent's object store.

    ``agent_path`` may be a subdirectory of the agent's repository (``mngr pair
    my-agent:/subdir``), so the fetch names that repository's worktree root: a git
    remote has to be the repository itself, unlike a working directory.
    """
    agent_git = git_context_for_host(host, cg)
    local_git = LocalGitContext(cg=cg)

    if not agent_git.is_git_repository(agent_path) or not local_git.is_git_repository(local_path):
        return None

    agent_branch = agent_git.get_current_branch(agent_path)
    local_branch = local_git.get_current_branch(local_path)

    agent_commit = agent_git.get_head_commit(agent_path)
    local_commit = local_git.get_head_commit(local_path)

    # Nothing to reconcile when either side has no commits yet, or both are on the
    # same one -- and this skips the fetch entirely in the common no-op case.
    if agent_commit is None or local_commit is None or agent_commit == local_commit:
        return GitSyncAction(
            agent_branch=agent_branch,
            local_branch=local_branch,
        )

    agent_repo_root = agent_git.get_repo_root(agent_path)
    if agent_repo_root is None:
        logger.warning("Could not find the agent's repository root for {}; skipping git sync", agent_path)
        return GitSyncAction(
            agent_branch=agent_branch,
            local_branch=local_branch,
        )

    try:
        git_fetch(
            local_path=local_path,
            remote_host=host,
            remote_path=agent_repo_root,
            extra_args=(agent_branch,),
            cg=cg,
            timeout_seconds=_GIT_FETCH_TIMEOUT_SECONDS,
        )
    except MngrError as e:
        logger.warning(
            "Failed to fetch from the agent for git sync comparison: {}",
            e,
        )
        return GitSyncAction(
            agent_branch=agent_branch,
            local_branch=local_branch,
        )

    # Check ancestry from the local repo, which now holds both sets of objects.
    agent_ahead = is_ancestor(local_path, local_commit, agent_commit, cg)
    local_ahead = is_ancestor(local_path, agent_commit, local_commit, cg)

    if agent_ahead and not local_ahead:
        return GitSyncAction(
            agent_is_ahead=True,
            agent_branch=agent_branch,
            local_branch=local_branch,
        )
    elif local_ahead and not agent_ahead:
        return GitSyncAction(
            local_is_ahead=True,
            agent_branch=agent_branch,
            local_branch=local_branch,
        )
    else:
        return GitSyncAction(
            agent_is_ahead=True,
            local_is_ahead=True,
            agent_branch=agent_branch,
            local_branch=local_branch,
        )


def _checkout(local_path: Path, branch: str, cg: ConcurrencyGroup) -> None:
    """Run ``git checkout`` in ``local_path``; raise MngrError on failure."""
    try:
        cg.run_process_to_completion(["git", "checkout", branch], cwd=local_path)
    except ProcessError as e:
        raise MngrError(f"Failed to checkout {branch}: {e.stderr}") from e


def _pull_agent_into_local(
    agent: AgentInterface,
    host: OnlineHostInterface,
    local_path: Path,
    agent_branch: str,
    local_branch: str,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> None:
    """Fetch agent_branch from the agent and merge it into local_branch.

    Stashes any uncommitted local changes (per ``uncommitted_changes``), checks
    out local_branch if it's not already current, merges, then restores the
    original branch on the way out.
    """
    local_git_ctx = LocalGitContext(cg=cg)
    with stash_guard(local_git_ctx, local_path, uncommitted_changes):
        original_branch = local_git_ctx.get_current_branch(local_path)
        did_switch = original_branch != local_branch
        if did_switch:
            _checkout(local_path, local_branch, cg)
        try:
            git_pull(
                local_path=local_path,
                remote_host=host,
                remote_path=agent.work_dir,
                extra_args=(agent_branch, "--no-edit"),
                cg=cg,
            )
        finally:
            if did_switch:
                _checkout(local_path, original_branch, cg)


def _push_local_to_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    local_path: Path,
    local_branch: str,
    agent_branch: str,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> None:
    """Push local_branch to the agent's agent_branch, stashing the agent's uncommitted changes first."""
    remote_git_ctx = RemoteGitContext(host=host)
    with stash_guard(remote_git_ctx, agent.work_dir, uncommitted_changes):
        git_push(
            local_path=local_path,
            remote_host=host,
            remote_path=agent.work_dir,
            extra_args=(f"{local_branch}:{agent_branch}",),
            cg=cg,
        )


def sync_git_state(
    agent: AgentInterface,
    host: OnlineHostInterface,
    local_path: Path,
    git_sync_action: GitSyncAction,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> tuple[bool, bool]:
    """Synchronize git state between agent and local paths.

    Returns (did_pull, did_push) indicating which operations were performed.
    """
    did_pull = False
    did_push = False

    if git_sync_action.agent_is_ahead:
        logger.debug("Pulling git state from agent to local")
        _pull_agent_into_local(
            agent=agent,
            host=host,
            local_path=local_path,
            agent_branch=git_sync_action.agent_branch,
            local_branch=git_sync_action.local_branch,
            uncommitted_changes=uncommitted_changes,
            cg=cg,
        )
        did_pull = True

    if git_sync_action.local_is_ahead:
        logger.debug("Pushing git state from local to agent")
        _push_local_to_agent(
            agent=agent,
            host=host,
            local_path=local_path,
            local_branch=git_sync_action.local_branch,
            agent_branch=git_sync_action.agent_branch,
            uncommitted_changes=uncommitted_changes,
            cg=cg,
        )
        did_push = True

    return did_pull, did_push


@contextmanager
def _remote_unison_transport(
    endpoint: SshEndpoint | None,
    host: OnlineHostInterface,
    cg: ConcurrencyGroup,
) -> Iterator[tuple[Path | None, Path | None]]:
    """Yield the ``-sshcmd`` wrapper path and ``-servercmd`` path for one pairing.

    Both are ``None`` for a local pairing, which runs a single unison process and
    so needs no transport and no version negotiation at all. For a remote one this
    checks the local version floor, resolves (installing if need be) a compatible
    unison on the host, and writes the ssh wrapper into a temporary directory that
    lives exactly as long as the sync.
    """
    if endpoint is None:
        yield None, None
        return

    # The version floor only applies once two unisons have to negotiate a protocol.
    check_local_unison_version(cg)
    remote_unison_path = ensure_remote_unison(host)
    with tempfile.TemporaryDirectory(prefix="mngr-pair-") as wrapper_dir:
        yield write_ssh_wrapper_script(endpoint, Path(wrapper_dir)), remote_unison_path


@contextmanager
def pair_files(
    agent: AgentInterface,
    host: OnlineHostInterface,
    agent_path: Path,
    local_path: Path,
    sync_direction: SyncDirection,
    conflict_mode: ConflictMode,
    is_require_git: bool,
    uncommitted_changes: UncommittedChangesMode,
    exclude_patterns: tuple[str, ...],
    include_patterns: tuple[str, ...],
    cg: ConcurrencyGroup,
) -> Iterator[UnisonSyncer]:
    """Start continuous file synchronization between agent and local directory.

    This function first synchronizes git state if both paths are git repositories,
    then starts a unison process for continuous file synchronization.

    The agent may be on a remote host. Because unison is a client/server protocol,
    that case additionally resolves a compatible unison on the host and builds an
    SSH transport for it; see ``mngr_pair.remote``.

    The returned context manager yields a UnisonSyncer that can be used to
    programmatically stop the sync. The sync is automatically stopped when
    the context manager exits.
    """
    require_unison()

    # A remote agent means unison must start a second unison on the far side over
    # SSH; a local agent is just two paths on this machine.
    endpoint = SshEndpoint.from_host(host)

    # Validate directories exist
    if not host.is_directory(agent_path):
        raise MngrError(f"Agent directory does not exist: {agent_path}")
    if not local_path.is_dir():
        raise MngrError(f"Local directory does not exist: {local_path}")

    # Validate agent and local are different directories. Only meaningful when the
    # agent is on this machine -- an identical path on another host is a different
    # directory.
    if host.is_local and agent_path.resolve() == local_path.resolve():
        raise MngrError(
            f"Agent and local are the same directory: {agent_path.resolve()}. "
            "Pair requires two different directories to sync between."
        )

    # Check git requirements
    agent_git = git_context_for_host(host, cg)
    local_git = LocalGitContext(cg=cg)
    agent_is_git = agent_git.is_git_repository(agent_path)
    local_is_git = local_git.is_git_repository(local_path)

    if is_require_git and not (agent_is_git and local_is_git):
        missing = []
        if not agent_is_git:
            missing.append(f"agent ({agent_path})")
        if not local_is_git:
            missing.append(f"local ({local_path})")
        raise MngrError(
            f"Git repositories required but not found in: {', '.join(missing)}. "
            "Use --no-require-git to sync without git."
        )

    # Resolve the transport before touching either repository. On a host mngr cannot
    # serve -- unison too old on this end, or an architecture upstream publishes no
    # build for -- pairing is impossible, and discovering that after the git
    # reconciliation would leave both repositories written to with no sync to show
    # for it.
    with _remote_unison_transport(endpoint, host, cg) as (ssh_wrapper_path, remote_unison_path):
        # Determine and perform git sync (skip when --no-require-git is set,
        # since the user explicitly opted out of git-based behavior)
        if is_require_git and agent_is_git and local_is_git:
            git_action = determine_git_sync_actions(agent_path, local_path, host, cg)
            if git_action is not None and (git_action.agent_is_ahead or git_action.local_is_ahead):
                logger.info(
                    "Synchronizing git state (agent_ahead={}, local_ahead={})",
                    git_action.agent_is_ahead,
                    git_action.local_is_ahead,
                )
                sync_git_state(
                    agent=agent,
                    host=host,
                    local_path=local_path,
                    git_sync_action=git_action,
                    uncommitted_changes=uncommitted_changes,
                    cg=cg,
                )

        syncer = UnisonSyncer(
            source_root=UnisonRoot(path=agent_path, ssh=endpoint),
            target_root=UnisonRoot(path=local_path),
            ssh_wrapper_path=ssh_wrapper_path,
            remote_unison_path=remote_unison_path,
            sync_direction=sync_direction,
            conflict_mode=conflict_mode,
            exclude_patterns=exclude_patterns,
            include_patterns=include_patterns,
            cg=cg,
        )

        try:
            syncer.start()
            yield syncer
        finally:
            # Stop unconditionally rather than on ``is_running``: that property is
            # False until unison has spoken, so a unison still in the SSH handshake
            # (and the ssh it spawned) would otherwise be left behind. ``stop`` is a
            # no-op when nothing was started.
            syncer.stop()
