import os
import shlex
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.errors import MngError
from imbue.mng.hosts.common import add_safe_directory_on_remote
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import CommandResult
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import SyncMode
from imbue.mng.primitives import UncommittedChangesMode
from imbue.mng.utils.deps import RSYNC
from imbue.mng.utils.git_utils import count_commits_between
from imbue.mng.utils.git_utils import get_current_branch
from imbue.mng.utils.git_utils import get_head_commit
from imbue.mng.utils.git_utils import is_ancestor
from imbue.mng.utils.git_utils import is_git_repository
from imbue.mng.utils.rsync_utils import parse_rsync_output

# Type alias for SSH connection info: (user, hostname, port, private_key_path)
SshConnectionInfo = tuple[str, str, int, Path]

# === Error Classes ===


class UncommittedChangesError(MngError):
    """Raised when there are uncommitted changes and mode is FAIL."""

    user_help_text = (
        "Use --uncommitted-changes=stash to stash changes before syncing, "
        "--uncommitted-changes=clobber to overwrite changes, "
        "or --uncommitted-changes=merge to stash, sync, then unstash."
    )

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        super().__init__(f"Uncommitted changes in destination: {destination}")


class NotAGitRepositoryError(MngError):
    """Raised when a git operation is attempted on a non-git directory."""

    user_help_text = (
        "Use --sync-mode=files to sync files without git, or ensure both source and destination are git repositories."
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Not a git repository: {path}")


class GitSyncError(MngError):
    """Raised when a git sync operation fails."""

    user_help_text = (
        "Check that the repository is accessible and you have the necessary permissions. "
        "You may need to resolve conflicts manually or use --uncommitted-changes=clobber."
    )

    def __init__(self, message: str) -> None:
        super().__init__(f"Git sync failed: {message}")


# === Result Classes ===


class SyncFilesResult(FrozenModel):
    """Result of a files sync operation."""

    files_transferred: int = Field(
        default=0,
        description="Number of files transferred",
    )
    bytes_transferred: int = Field(
        default=0,
        description="Total bytes transferred",
    )
    source_path: Path = Field(
        description="Source path",
    )
    destination_path: Path = Field(
        description="Destination path",
    )
    is_dry_run: bool = Field(
        default=False,
        description="Whether this was a dry run",
    )
    mode: SyncMode = Field(
        description="Direction of the sync operation",
    )


class SyncGitResult(FrozenModel):
    """Result of a git sync operation."""

    source_branch: str = Field(
        description="Branch that was synced from",
    )
    target_branch: str = Field(
        description="Branch that was synced to",
    )
    source_path: Path = Field(
        description="Source repository path",
    )
    destination_path: Path = Field(
        description="Destination repository path",
    )
    is_dry_run: bool = Field(
        default=False,
        description="Whether this was a dry run",
    )
    commits_transferred: int = Field(
        default=0,
        description="Number of commits transferred",
    )
    mode: SyncMode = Field(
        description="Direction of the sync operation",
    )


# === Git Context Interface and Implementations ===


class GitContextInterface(MutableModel, ABC):
    """Interface for executing git commands either locally or on a remote host."""

    @abstractmethod
    def has_uncommitted_changes(self, path: Path) -> bool:
        """Check if the path has uncommitted git changes."""

    @abstractmethod
    def git_stash(self, path: Path) -> bool:
        """Stash uncommitted changes. Returns True if something was stashed."""

    @abstractmethod
    def git_stash_pop(self, path: Path) -> None:
        """Pop the most recent stash."""

    @abstractmethod
    def git_reset_hard(self, path: Path) -> None:
        """Hard reset to discard all uncommitted changes."""

    @abstractmethod
    def get_current_branch(self, path: Path) -> str:
        """Get the current branch name."""

    @abstractmethod
    def is_git_repository(self, path: Path) -> bool:
        """Check if the path is inside a git repository."""


class LocalGitContext(GitContextInterface):
    """Execute git commands locally via ConcurrencyGroup."""

    cg: ConcurrencyGroup = Field(frozen=True, description="Concurrency group for process management")

    def has_uncommitted_changes(self, path: Path) -> bool:
        try:
            result = self.cg.run_process_to_completion(
                ["git", "status", "--porcelain"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngError(f"git status failed in {path}: {e.stderr}") from e
        return len(result.stdout.strip()) > 0

    def git_stash(self, path: Path) -> bool:
        try:
            result = self.cg.run_process_to_completion(
                ["git", "stash", "push", "-u", "-m", "mng-sync-stash"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngError(f"git stash failed: {e.stderr}") from e
        return "No local changes to save" not in result.stdout

    def git_stash_pop(self, path: Path) -> None:
        try:
            self.cg.run_process_to_completion(
                ["git", "stash", "pop"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngError(f"git stash pop failed: {e.stderr}") from e

    def git_reset_hard(self, path: Path) -> None:
        try:
            self.cg.run_process_to_completion(
                ["git", "reset", "--hard", "HEAD"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngError(f"git reset --hard failed: {e.stderr}") from e
        try:
            self.cg.run_process_to_completion(
                ["git", "clean", "-fd"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngError(f"git clean failed: {e.stderr}") from e

    def get_current_branch(self, path: Path) -> str:
        return get_current_branch(path, self.cg)

    def is_git_repository(self, path: Path) -> bool:
        return is_git_repository(path, self.cg)


class RemoteGitContext(GitContextInterface):
    """Execute git commands on a remote host via host.execute_command."""

    _host: OnlineHostInterface = PrivateAttr()

    def __init__(self, *, host: OnlineHostInterface) -> None:
        super().__init__()
        self._host = host

    @property
    def host(self) -> OnlineHostInterface:
        """The host to execute commands on."""
        return self._host

    def has_uncommitted_changes(self, path: Path) -> bool:
        result = self._host.execute_command("git status --porcelain", cwd=path)
        if not result.success:
            raise MngError(f"git status failed in {path}: {result.stderr}")
        return len(result.stdout.strip()) > 0

    def git_stash(self, path: Path) -> bool:
        result = self._host.execute_command(
            'git stash push -u -m "mng-sync-stash"',
            cwd=path,
        )
        if not result.success:
            raise MngError(f"git stash failed: {result.stderr}")
        return "No local changes to save" not in result.stdout

    def git_stash_pop(self, path: Path) -> None:
        result = self._host.execute_command("git stash pop", cwd=path)
        if not result.success:
            raise MngError(f"git stash pop failed: {result.stderr}")

    def git_reset_hard(self, path: Path) -> None:
        result = self._host.execute_command("git reset --hard HEAD", cwd=path)
        if not result.success:
            raise MngError(f"git reset --hard failed: {result.stderr}")
        result = self._host.execute_command("git clean -fd", cwd=path)
        if not result.success:
            raise MngError(f"git clean failed: {result.stderr}")

    def get_current_branch(self, path: Path) -> str:
        result = self._host.execute_command("git rev-parse --abbrev-ref HEAD", cwd=path)
        if not result.success:
            raise MngError(f"Failed to get current branch: {result.stderr}")
        return result.stdout.strip()

    def is_git_repository(self, path: Path) -> bool:
        result = self._host.execute_command("git rev-parse --git-dir", cwd=path)
        return result.success


# === Uncommitted Changes Handling ===


def handle_uncommitted_changes(
    git_ctx: GitContextInterface,
    path: Path,
    uncommitted_changes: UncommittedChangesMode,
) -> bool:
    """Handle uncommitted changes according to the specified mode.

    Returns True if changes were stashed (and may need to be restored).
    """
    is_uncommitted = git_ctx.has_uncommitted_changes(path)

    if not is_uncommitted:
        return False

    match uncommitted_changes:
        case UncommittedChangesMode.FAIL:
            raise UncommittedChangesError(path)
        case UncommittedChangesMode.STASH:
            logger.debug("Stashing uncommitted changes")
            return git_ctx.git_stash(path)
        case UncommittedChangesMode.MERGE:
            logger.debug("Stashing uncommitted changes for merge")
            return git_ctx.git_stash(path)
        case UncommittedChangesMode.CLOBBER:
            logger.debug("Clobbering uncommitted changes")
            git_ctx.git_reset_hard(path)
            return False
        case _ as unreachable:
            assert_never(unreachable)


@contextmanager
def _stash_guard(
    git_ctx: GitContextInterface,
    path: Path,
    uncommitted_changes: UncommittedChangesMode,
) -> Iterator[bool]:
    """Context manager that stashes/pops around a sync operation.

    Yields True if changes were stashed. On normal exit, pops stash if mode is
    MERGE. On exception, attempts to pop stash for MERGE mode with a warning on
    failure.
    """
    did_stash = handle_uncommitted_changes(git_ctx, path, uncommitted_changes)
    is_success = False
    try:
        yield did_stash
        is_success = True
    finally:
        if did_stash and uncommitted_changes == UncommittedChangesMode.MERGE:
            if is_success:
                logger.debug("Restoring stashed changes")
                git_ctx.git_stash_pop(path)
            else:
                try:
                    git_ctx.git_stash_pop(path)
                except MngError:
                    logger.warning(
                        "Failed to restore stashed changes after sync failure. "
                        "Run 'git stash pop' in {} to recover your changes.",
                        path,
                    )


# === File Sync Functions ===


@pure
def _build_rsync_command(
    source_path: Path,
    destination_path: Path,
    is_dry_run: bool,
    is_delete: bool,
) -> list[str]:
    """Build an rsync command for file synchronization."""
    rsync_cmd = ["rsync", "-avz", "--stats", "--exclude=.git"]

    if is_dry_run:
        rsync_cmd.append("--dry-run")

    if is_delete:
        rsync_cmd.append("--delete")

    # Add trailing slash to source to copy contents, not the directory itself
    source_str = str(source_path)
    if not source_str.endswith("/"):
        source_str += "/"

    rsync_cmd.append(source_str)
    rsync_cmd.append(str(destination_path))

    return rsync_cmd


@pure
def _build_ssh_transport_args(ssh_info: SshConnectionInfo) -> str:
    """Build the SSH transport string for rsync -e or GIT_SSH_COMMAND."""
    user, hostname, port, key_path = ssh_info
    return f"ssh -i {shlex.quote(str(key_path))} -p {port} -o StrictHostKeyChecking=no"


@pure
def _build_ssh_git_url(ssh_info: SshConnectionInfo, remote_path: Path) -> str:
    """Build an SSH git URL from connection info and a remote path."""
    user, hostname, port, key_path = ssh_info
    return f"ssh://{user}@{hostname}:{port}{remote_path}/.git"


@pure
def _build_remote_rsync_command(
    source_path: Path,
    destination_path: Path,
    ssh_info: SshConnectionInfo,
    mode: SyncMode,
    is_dry_run: bool,
    is_delete: bool,
) -> list[str]:
    """Build an rsync command that transfers files over SSH to/from a remote host."""
    user, hostname, port, key_path = ssh_info
    ssh_transport = _build_ssh_transport_args(ssh_info)

    rsync_cmd = ["rsync", "-avz", "--stats", "--exclude=.git", "-e", ssh_transport]

    if is_dry_run:
        rsync_cmd.append("--dry-run")

    if is_delete:
        rsync_cmd.append("--delete")

    match mode:
        case SyncMode.PUSH:
            # Local source -> remote destination
            source_str = str(source_path)
            if not source_str.endswith("/"):
                source_str += "/"
            dest_str = str(destination_path)
            if not dest_str.endswith("/"):
                dest_str += "/"
            rsync_cmd.append(source_str)
            rsync_cmd.append(f"{user}@{hostname}:{dest_str}")
        case SyncMode.PULL:
            # Remote source -> local destination
            source_str = str(source_path)
            if not source_str.endswith("/"):
                source_str += "/"
            rsync_cmd.append(f"{user}@{hostname}:{source_str}")
            rsync_cmd.append(str(destination_path))
        case _ as unreachable:
            assert_never(unreachable)

    return rsync_cmd


def sync_files(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mode: SyncMode,
    local_path: Path,
    remote_path: Path | None,
    is_dry_run: bool,
    is_delete: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> SyncFilesResult:
    """Sync files between local and agent using rsync."""
    RSYNC.require()

    actual_remote_path = remote_path if remote_path is not None else agent.work_dir

    # Determine source and destination based on mode
    match mode:
        case SyncMode.PUSH:
            source_path = local_path
            destination_path = actual_remote_path
            git_ctx: GitContextInterface = RemoteGitContext(host=host)
        case SyncMode.PULL:
            source_path = actual_remote_path
            destination_path = local_path
            git_ctx = LocalGitContext(cg=cg)
        case _ as unreachable:
            assert_never(unreachable)

    # Handle uncommitted changes in the destination.
    # CLOBBER mode skips this check entirely in the file sync path -- it means
    # "proceed with rsync regardless of uncommitted changes" and overwrites files
    # in-place. This differs from handle_uncommitted_changes with CLOBBER in the
    # git sync path, where CLOBBER calls git_reset_hard to discard changes before
    # merging.
    #
    # Also skip if the destination doesn't exist yet (e.g. pushing to a new
    # subdirectory) or isn't a git repo (e.g. pulling to a plain directory).
    # In PUSH mode the destination is on the remote host; in PULL mode it is local.
    is_destination_exists = _dir_exists(host, destination_path) if mode == SyncMode.PUSH else destination_path.is_dir()
    is_destination_git_repo = git_ctx.is_git_repository(destination_path) if is_destination_exists else False
    should_stash = uncommitted_changes != UncommittedChangesMode.CLOBBER and is_destination_git_repo

    stash_cm = _stash_guard(git_ctx, destination_path, uncommitted_changes) if should_stash else nullcontext(False)

    with stash_cm:
        # Ensure destination directory exists for push mode subdirectory targets
        if mode == SyncMode.PUSH and not _dir_exists(host, destination_path):
            if host.is_local:
                destination_path.mkdir(parents=True, exist_ok=True)
            else:
                mkdir_result = host.execute_command(f"mkdir -p {shlex.quote(str(destination_path))}")
                if not mkdir_result.success:
                    raise MngError(f"Failed to create remote directory {destination_path}: {mkdir_result.stderr}")

        direction = "Pushing" if mode == SyncMode.PUSH else "Pulling"

        if host.is_local:
            # Local host: run rsync via host.execute_command (both paths are local)
            rsync_cmd = _build_rsync_command(source_path, destination_path, is_dry_run, is_delete)
            cmd_str = shlex.join(rsync_cmd)

            with log_span("{} files from {} to {}", direction, source_path, destination_path):
                logger.debug("Running rsync command: {}", cmd_str)
                result: CommandResult = host.execute_command(cmd_str)

            if not result.success:
                raise MngError(f"rsync failed: {result.stderr}")

            rsync_stdout = result.stdout
        else:
            # Remote host: run rsync locally with SSH transport
            ssh_info = host.get_ssh_connection_info()
            assert ssh_info is not None, "Remote host must provide SSH connection info"

            rsync_cmd = _build_remote_rsync_command(
                source_path=source_path,
                destination_path=destination_path,
                ssh_info=ssh_info,
                mode=mode,
                is_dry_run=is_dry_run,
                is_delete=is_delete,
            )

            with log_span("{} files from {} to {} via SSH", direction, source_path, destination_path):
                logger.debug("Running rsync command: {}", shlex.join(rsync_cmd))
                try:
                    process_result = cg.run_process_to_completion(rsync_cmd)
                except ProcessError as e:
                    raise MngError(f"rsync failed: {e.stderr}") from e

            rsync_stdout = process_result.stdout

        # Parse rsync output to extract statistics
        files_transferred, bytes_transferred = parse_rsync_output(rsync_stdout)

    logger.debug(
        "Sync complete: {} files, {} bytes transferred{}",
        files_transferred,
        bytes_transferred,
        " (dry run)" if is_dry_run else "",
    )

    return SyncFilesResult(
        files_transferred=files_transferred,
        bytes_transferred=bytes_transferred,
        source_path=source_path,
        destination_path=destination_path,
        is_dry_run=is_dry_run,
        mode=mode,
    )


def _dir_exists(host: OnlineHostInterface, path: Path) -> bool:
    """Check if a directory exists on the given host."""
    if host.is_local:
        return path.is_dir()
    result = host.execute_command(f"test -d {shlex.quote(str(path))}")
    return result.success


# === Git Sync Helper Functions ===


def _get_head_commit_or_raise(path: Path, cg: ConcurrencyGroup) -> str:
    """Get the current HEAD commit hash, raising on failure."""
    commit = get_head_commit(path, cg)
    if commit is None:
        raise MngError(f"Failed to get HEAD commit in {path}")
    return commit


def _merge_fetch_head(local_path: Path, cg: ConcurrencyGroup) -> None:
    """Merge FETCH_HEAD into the current branch, aborting on conflict."""
    try:
        cg.run_process_to_completion(
            ["git", "merge", "FETCH_HEAD", "--no-edit"],
            cwd=local_path,
        )
    except ProcessError as merge_error:
        # Check if a merge is actually in progress before trying to abort
        try:
            cg.run_process_to_completion(
                ["git", "rev-parse", "--verify", "MERGE_HEAD"],
                cwd=local_path,
            )
            # MERGE_HEAD exists, so a merge is in progress - abort it
            try:
                cg.run_process_to_completion(
                    ["git", "merge", "--abort"],
                    cwd=local_path,
                )
            except ProcessError as abort_error:
                logger.warning(
                    "Failed to abort merge in {}: {}. Repository may be in a conflicted state.",
                    local_path,
                    abort_error.stderr.strip(),
                )
        except ProcessError:
            # No MERGE_HEAD means no merge in progress, nothing to abort
            pass
        raise GitSyncError(merge_error.stderr) from merge_error


# === Git Push Functions ===


def _local_git_push_mirror(
    local_path: Path,
    destination_path: Path,
    host: OnlineHostInterface,
    source_branch: str,
    is_dry_run: bool,
    cg: ConcurrencyGroup,
) -> int:
    """Push via mirror fetch, overwriting all refs in the target.

    Returns the number of commits transferred.
    """
    target_git_dir = str(destination_path)
    logger.debug("Performing mirror fetch to {}", target_git_dir)

    pre_fetch_head = get_head_commit(destination_path, cg)

    if is_dry_run:
        # Estimate using pre_fetch_head (the agent's current HEAD). target_branch
        # is a branch name that may not exist locally, but pre_fetch_head is a
        # commit hash valid in both repos since local agents share the same git
        # object store.
        if pre_fetch_head is not None:
            return count_commits_between(local_path, pre_fetch_head, source_branch, cg)
        return 0

    # Fetch all refs from source into target. --update-head-ok is needed because
    # git otherwise refuses to fetch into the currently checked-out branch.
    try:
        cg.run_process_to_completion(
            [
                "git",
                "-C",
                target_git_dir,
                "fetch",
                "--update-head-ok",
                str(local_path),
                "--force",
                "refs/*:refs/*",
            ],
        )
    except ProcessError as e:
        raise GitSyncError(e.stderr) from e

    # Reset working tree to match the source branch content. We do NOT checkout
    # source_branch because in the worktree case (local agents), the source
    # branch may already be checked out in the source worktree, and git forbids
    # two worktrees from having the same branch checked out.
    reset_result = host.execute_command(
        f"git reset --hard refs/heads/{source_branch}",
        cwd=destination_path,
    )
    if not reset_result.success:
        raise GitSyncError(f"Failed to update working tree: {reset_result.stderr}")

    # Count actual commits transferred by comparing pre/post HEAD
    post_fetch_head = get_head_commit(destination_path, cg)
    if pre_fetch_head is not None and post_fetch_head is not None and pre_fetch_head != post_fetch_head:
        return count_commits_between(destination_path, pre_fetch_head, post_fetch_head, cg)
    return 0


def _local_git_push_branch(
    local_path: Path,
    destination_path: Path,
    host: OnlineHostInterface,
    source_branch: str,
    target_branch: str,
    is_dry_run: bool,
    cg: ConcurrencyGroup,
) -> int:
    """Push a single branch via fetch+reset.

    Returns the number of commits transferred.
    """
    target_git_dir = str(destination_path)
    logger.debug("Fetching branch {} into {}", source_branch, target_git_dir)

    pre_fetch_head = get_head_commit(destination_path, cg)

    if is_dry_run:
        if pre_fetch_head is not None:
            return count_commits_between(local_path, pre_fetch_head, source_branch, cg)
        return 0

    # Fetch from source repo into the target
    try:
        cg.run_process_to_completion(
            ["git", "-C", target_git_dir, "fetch", str(local_path), source_branch],
        )
    except ProcessError as e:
        raise GitSyncError(e.stderr) from e

    # Resolve FETCH_HEAD to an explicit commit hash to avoid race conditions
    try:
        hash_result = cg.run_process_to_completion(
            ["git", "-C", target_git_dir, "rev-parse", "FETCH_HEAD"],
        )
    except ProcessError as e:
        raise GitSyncError(f"Failed to resolve FETCH_HEAD: {e.stderr}") from e
    fetched_commit = hash_result.stdout.strip()

    # Check for non-fast-forward push (diverged history)
    if pre_fetch_head is not None and pre_fetch_head != fetched_commit:
        is_fast_forward = is_ancestor(destination_path, pre_fetch_head, fetched_commit, cg)
        if not is_fast_forward:
            raise GitSyncError(
                f"Cannot push: agent branch '{target_branch}' has diverged from "
                f"local branch '{source_branch}'. Use --mirror to force-overwrite "
                f"all refs, or pull agent changes first to reconcile."
            )

    # Reset the target branch to the fetched commit
    reset_result = host.execute_command(
        f"git reset --hard {fetched_commit}",
        cwd=destination_path,
    )
    if not reset_result.success:
        raise GitSyncError(f"Failed to update working tree: {reset_result.stderr}")

    # Count actual commits transferred
    commits_transferred = 0
    if pre_fetch_head is not None and pre_fetch_head != fetched_commit:
        commits_transferred = count_commits_between(destination_path, pre_fetch_head, fetched_commit, cg)

    logger.debug(
        "Git push complete: pushed {} commits from {} to {}",
        commits_transferred,
        source_branch,
        target_branch,
    )
    return commits_transferred


def _remote_git_push_mirror(
    local_path: Path,
    destination_path: Path,
    host: OnlineHostInterface,
    ssh_info: SshConnectionInfo,
    source_branch: str,
    is_dry_run: bool,
    cg: ConcurrencyGroup,
) -> int:
    """Push via git push --mirror over SSH, overwriting all refs in the target.

    Returns the number of commits transferred.
    """
    git_url = _build_ssh_git_url(ssh_info, destination_path)
    git_ssh_cmd = _build_ssh_transport_args(ssh_info)
    env = {**os.environ, "GIT_SSH_COMMAND": git_ssh_cmd, "GIT_LFS_SKIP_PUSH": "1"}

    logger.debug("Performing mirror push to {}", git_url)

    # Get pre-push HEAD from the remote
    pre_push_head_result = host.execute_command("git rev-parse HEAD", cwd=destination_path)
    pre_push_head = pre_push_head_result.stdout.strip() if pre_push_head_result.success else None

    if is_dry_run:
        # Estimate commits by fetching remote HEAD locally and counting
        if pre_push_head is not None:
            return count_commits_between(local_path, pre_push_head, source_branch, cg)
        return 0

    # Configure remote to accept mirror pushes. Mirror push deletes branches
    # that don't exist locally, which may include the remote's checked-out
    # branch. Detach HEAD first so no branch is "checked out", allowing all
    # branches to be freely updated or deleted.
    for config_cmd in [
        "git config receive.denyCurrentBranch updateInstead",
        "git config receive.denyDeleteCurrent ignore",
        "git checkout --detach HEAD",
    ]:
        config_result = host.execute_command(config_cmd, cwd=destination_path)
        if not config_result.success:
            raise GitSyncError(f"Failed to configure remote for mirror push: {config_result.stderr}")

    # Push all refs from local to remote
    try:
        cg.run_process_to_completion(
            ["git", "-C", str(local_path), "push", "--no-verify", "--mirror", "--force", git_url],
            env=env,
        )
    except ProcessError as e:
        raise GitSyncError(e.stderr) from e

    # Reset working tree on remote to match the source branch
    reset_result = host.execute_command(
        f"git reset --hard refs/heads/{source_branch}",
        cwd=destination_path,
    )
    if not reset_result.success:
        raise GitSyncError(f"Failed to update working tree: {reset_result.stderr}")

    # Count actual commits transferred by comparing pre/post HEAD
    post_push_head_result = host.execute_command("git rev-parse HEAD", cwd=destination_path)
    post_push_head = post_push_head_result.stdout.strip() if post_push_head_result.success else None

    if pre_push_head is not None and post_push_head is not None and pre_push_head != post_push_head:
        return count_commits_between(local_path, pre_push_head, post_push_head, cg)
    return 0


def _remote_git_push_branch(
    local_path: Path,
    destination_path: Path,
    host: OnlineHostInterface,
    ssh_info: SshConnectionInfo,
    source_branch: str,
    target_branch: str,
    is_dry_run: bool,
    cg: ConcurrencyGroup,
) -> int:
    """Push a single branch to a remote host via git push over SSH.

    Returns the number of commits transferred.
    """
    git_url = _build_ssh_git_url(ssh_info, destination_path)
    git_ssh_cmd = _build_ssh_transport_args(ssh_info)
    env = {**os.environ, "GIT_SSH_COMMAND": git_ssh_cmd, "GIT_LFS_SKIP_PUSH": "1"}

    logger.debug("Pushing branch {} to {} via SSH", source_branch, git_url)

    # Get pre-push HEAD from the remote
    pre_push_head_result = host.execute_command("git rev-parse HEAD", cwd=destination_path)
    pre_push_head = pre_push_head_result.stdout.strip() if pre_push_head_result.success else None

    if is_dry_run:
        if pre_push_head is not None:
            return count_commits_between(local_path, pre_push_head, source_branch, cg)
        return 0

    # Configure remote to accept pushes to the checked-out branch
    config_result = host.execute_command(
        "git config receive.denyCurrentBranch updateInstead",
        cwd=destination_path,
    )
    if not config_result.success:
        raise GitSyncError(f"Failed to configure remote: {config_result.stderr}")

    # Check for non-fast-forward push before attempting the push.
    # Fetch the remote HEAD locally to check ancestry.
    if pre_push_head is not None:
        is_fast_forward = is_ancestor(local_path, pre_push_head, source_branch, cg)
        if not is_fast_forward:
            raise GitSyncError(
                f"Cannot push: agent branch '{target_branch}' has diverged from "
                f"local branch '{source_branch}'. Use --mirror to force-overwrite "
                f"all refs, or pull agent changes first to reconcile."
            )

    # Push the branch to the remote
    try:
        cg.run_process_to_completion(
            ["git", "-C", str(local_path), "push", git_url, f"{source_branch}:{target_branch}"],
            env=env,
        )
    except ProcessError as e:
        raise GitSyncError(e.stderr) from e

    # Count actual commits transferred
    post_push_head_result = host.execute_command("git rev-parse HEAD", cwd=destination_path)
    post_push_head = post_push_head_result.stdout.strip() if post_push_head_result.success else None

    commits_transferred = 0
    if pre_push_head is not None and post_push_head is not None and pre_push_head != post_push_head:
        commits_transferred = count_commits_between(local_path, pre_push_head, post_push_head, cg)

    logger.debug(
        "Git push complete: pushed {} commits from {} to {}",
        commits_transferred,
        source_branch,
        target_branch,
    )
    return commits_transferred


def _sync_git_push(
    agent: AgentInterface,
    host: OnlineHostInterface,
    local_path: Path,
    source_branch: str,
    target_branch: str,
    is_dry_run: bool,
    uncommitted_changes: UncommittedChangesMode,
    is_mirror: bool,
    cg: ConcurrencyGroup,
) -> SyncGitResult:
    """Push git commits from local to agent repository."""
    destination_path = agent.work_dir
    git_ctx = RemoteGitContext(host=host)

    with _stash_guard(git_ctx, destination_path, uncommitted_changes):
        if host.is_local:
            if is_mirror:
                commits_transferred = _local_git_push_mirror(
                    local_path,
                    destination_path,
                    host,
                    source_branch,
                    is_dry_run,
                    cg,
                )
            else:
                commits_transferred = _local_git_push_branch(
                    local_path,
                    destination_path,
                    host,
                    source_branch,
                    target_branch,
                    is_dry_run,
                    cg,
                )
        else:
            ssh_info = host.get_ssh_connection_info()
            assert ssh_info is not None, "Remote host must provide SSH connection info"
            if is_mirror:
                commits_transferred = _remote_git_push_mirror(
                    local_path,
                    destination_path,
                    host,
                    ssh_info,
                    source_branch,
                    is_dry_run,
                    cg,
                )
            else:
                commits_transferred = _remote_git_push_branch(
                    local_path,
                    destination_path,
                    host,
                    ssh_info,
                    source_branch,
                    target_branch,
                    is_dry_run,
                    cg,
                )

    return SyncGitResult(
        source_branch=source_branch,
        target_branch=target_branch,
        source_path=local_path,
        destination_path=destination_path,
        is_dry_run=is_dry_run,
        commits_transferred=commits_transferred,
        mode=SyncMode.PUSH,
    )


# === Git Pull Functions ===


def _fetch_and_merge(
    local_path: Path,
    source_path: Path,
    source_branch: str,
    target_branch: str,
    original_branch: str,
    is_dry_run: bool,
    cg: ConcurrencyGroup,
    ssh_info: SshConnectionInfo | None,
) -> int:
    """Fetch from source repo and merge into target branch.

    Handles checkout to target_branch if different from original_branch, and
    restores original_branch on both success and failure. Returns the number
    of commits transferred.

    When ssh_info is provided, fetches from the remote host via SSH URL instead
    of a local path.
    """
    # Fetch from the agent's repository (sets FETCH_HEAD)
    if ssh_info is not None:
        # Remote host: fetch via SSH URL
        git_url = _build_ssh_git_url(ssh_info, source_path)
        git_ssh_cmd = _build_ssh_transport_args(ssh_info)
        fetch_env = {**os.environ, "GIT_SSH_COMMAND": git_ssh_cmd}
        logger.debug("Fetching from remote agent repository via SSH: {}", git_url)
        try:
            cg.run_process_to_completion(
                ["git", "fetch", git_url, source_branch],
                cwd=local_path,
                env=fetch_env,
            )
        except ProcessError as e:
            raise MngError(f"Failed to fetch from agent: {e.stderr}") from e
    else:
        # Local host: fetch from local path
        logger.debug("Fetching from agent repository: {}", source_path)
        try:
            cg.run_process_to_completion(
                ["git", "fetch", str(source_path), source_branch],
                cwd=local_path,
            )
        except ProcessError as e:
            raise MngError(f"Failed to fetch from agent: {e.stderr}") from e

    # Checkout the target branch if different from current
    did_checkout = original_branch != target_branch
    if did_checkout:
        logger.debug("Checking out target branch: {}", target_branch)
        try:
            cg.run_process_to_completion(
                ["git", "checkout", target_branch],
                cwd=local_path,
            )
        except ProcessError as e:
            raise MngError(f"Failed to checkout target branch: {e.stderr}") from e

    # Record HEAD after checkout so we count commits on the target branch
    pre_merge_head = _get_head_commit_or_raise(local_path, cg)
    commits_to_merge = count_commits_between(local_path, "HEAD", "FETCH_HEAD", cg)

    try:
        if is_dry_run:
            logger.debug(
                "Dry run: would merge {} commits from {} into {}",
                commits_to_merge,
                source_branch,
                target_branch,
            )
            commits_transferred = commits_to_merge
        else:
            _merge_fetch_head(local_path, cg)
            post_merge_head = _get_head_commit_or_raise(local_path, cg)
            commits_transferred = (
                count_commits_between(local_path, pre_merge_head, post_merge_head, cg)
                if pre_merge_head != post_merge_head
                else 0
            )
            logger.debug(
                "Git pull complete: merged {} commits from {} into {}",
                commits_transferred,
                source_branch,
                target_branch,
            )
    except MngError:
        # On failure, try to restore original branch before re-raising
        if did_checkout:
            try:
                cg.run_process_to_completion(
                    ["git", "checkout", original_branch],
                    cwd=local_path,
                )
            except ProcessError as checkout_error:
                logger.warning(
                    "Failed to restore branch {} after git pull failure: {}",
                    original_branch,
                    checkout_error.stderr.strip(),
                )
        raise

    # Restore original branch on success
    if did_checkout:
        try:
            cg.run_process_to_completion(
                ["git", "checkout", original_branch],
                cwd=local_path,
            )
        except ProcessError as e:
            raise MngError(f"Failed to checkout original branch {original_branch}: {e.stderr}") from e

    return commits_transferred


def _sync_git_pull(
    agent: AgentInterface,
    host: OnlineHostInterface,
    local_path: Path,
    source_branch: str,
    target_branch: str,
    is_dry_run: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> SyncGitResult:
    """Pull git commits from agent to local repository."""
    source_path = agent.work_dir
    git_ctx = LocalGitContext(cg=cg)
    original_branch = get_current_branch(local_path, cg)

    # Get SSH info for remote hosts so _fetch_and_merge can use SSH URLs
    ssh_info = host.get_ssh_connection_info() if not host.is_local else None

    with _stash_guard(git_ctx, local_path, uncommitted_changes):
        commits_transferred = _fetch_and_merge(
            local_path=local_path,
            source_path=source_path,
            source_branch=source_branch,
            target_branch=target_branch,
            original_branch=original_branch,
            is_dry_run=is_dry_run,
            cg=cg,
            ssh_info=ssh_info,
        )

    return SyncGitResult(
        source_branch=source_branch,
        target_branch=target_branch,
        source_path=source_path,
        destination_path=local_path,
        is_dry_run=is_dry_run,
        commits_transferred=commits_transferred,
        mode=SyncMode.PULL,
    )


# === Top-Level Git Sync ===


def sync_git(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mode: SyncMode,
    local_path: Path,
    source_branch: str | None,
    target_branch: str | None,
    is_dry_run: bool,
    uncommitted_changes: UncommittedChangesMode,
    is_mirror: bool,
    cg: ConcurrencyGroup,
) -> SyncGitResult:
    """Sync git commits between local and agent."""
    remote_path = agent.work_dir
    local_git_ctx = LocalGitContext(cg=cg)
    remote_git_ctx = RemoteGitContext(host=host)

    logger.debug("Syncing git from {} to {} (mode={})", local_path, remote_path, mode)

    add_safe_directory_on_remote(host, remote_path)

    # Verify both are git repositories
    if not local_git_ctx.is_git_repository(local_path):
        raise NotAGitRepositoryError(local_path)

    if not remote_git_ctx.is_git_repository(remote_path):
        raise NotAGitRepositoryError(remote_path)

    match mode:
        case SyncMode.PUSH:
            # Push: local -> agent
            actual_source_branch = (
                source_branch if source_branch is not None else local_git_ctx.get_current_branch(local_path)
            )
            actual_target_branch = (
                target_branch if target_branch is not None else remote_git_ctx.get_current_branch(remote_path)
            )

            return _sync_git_push(
                agent=agent,
                host=host,
                local_path=local_path,
                source_branch=actual_source_branch,
                target_branch=actual_target_branch,
                is_dry_run=is_dry_run,
                uncommitted_changes=uncommitted_changes,
                is_mirror=is_mirror,
                cg=cg,
            )
        case SyncMode.PULL:
            # Pull: agent -> local
            actual_source_branch = (
                source_branch if source_branch is not None else remote_git_ctx.get_current_branch(remote_path)
            )
            actual_target_branch = (
                target_branch if target_branch is not None else local_git_ctx.get_current_branch(local_path)
            )

            if is_mirror:
                raise NotImplementedError("Mirror mode is only supported for push operations")

            return _sync_git_pull(
                agent=agent,
                host=host,
                local_path=local_path,
                source_branch=actual_source_branch,
                target_branch=actual_target_branch,
                is_dry_run=is_dry_run,
                uncommitted_changes=uncommitted_changes,
                cg=cg,
            )
        case _ as unreachable:
            assert_never(unreachable)
