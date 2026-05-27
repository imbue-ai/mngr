import os
import shlex
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import add_safe_directory_on_remote
from imbue.mngr.hosts.common import build_ssh_transport_command
from imbue.mngr.hosts.common import get_ssh_known_hosts_file
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.deps import RSYNC
from imbue.mngr.utils.git_utils import get_current_branch
from imbue.mngr.utils.git_utils import is_git_repository
from imbue.mngr.utils.rsync_utils import parse_rsync_output

# Type alias for SSH connection info: (user, hostname, port, private_key_path)
SshConnectionInfo = tuple[str, str, int, Path]

# === Error Classes ===


class UncommittedChangesError(MngrError):
    """Raised when there are uncommitted changes and mode is FAIL."""

    user_help_text = (
        "Use --uncommitted-changes=stash to stash changes before syncing, "
        "--uncommitted-changes=clobber to overwrite changes, "
        "or --uncommitted-changes=merge to stash, sync, then unstash."
    )

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        super().__init__(f"Uncommitted changes in destination: {destination}")


class GitSyncError(MngrError):
    """Raised when a git push or pull subprocess fails."""

    user_help_text = "Check that the repository is accessible and you have the necessary permissions."

    def __init__(self, message: str) -> None:
        super().__init__(f"Git sync failed: {message}")


class RsyncEndpointError(UserInputError):
    """Raised when an rsync invocation has both endpoints on the same locality (both local or both remote)."""

    user_help_text = (
        "``mngr rsync`` requires exactly one endpoint to be on the local machine and the other on a "
        "remote host. Use ``rsync`` directly for local-to-local copies."
    )


# === Result Classes ===


class RsyncResult(FrozenModel):
    """Result of an rsync operation between two endpoints."""

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
            raise MngrError(f"git status failed in {path}: {e.stderr}") from e
        return len(result.stdout.strip()) > 0

    def git_stash(self, path: Path) -> bool:
        try:
            result = self.cg.run_process_to_completion(
                ["git", "stash", "push", "-u", "-m", "mngr-sync-stash"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngrError(f"git stash failed: {e.stderr}") from e
        return "No local changes to save" not in result.stdout

    def git_stash_pop(self, path: Path) -> None:
        try:
            self.cg.run_process_to_completion(
                ["git", "stash", "pop"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngrError(f"git stash pop failed: {e.stderr}") from e

    def git_reset_hard(self, path: Path) -> None:
        try:
            self.cg.run_process_to_completion(
                ["git", "reset", "--hard", "HEAD"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngrError(f"git reset --hard failed: {e.stderr}") from e
        try:
            self.cg.run_process_to_completion(
                ["git", "clean", "-fd"],
                cwd=path,
            )
        except ProcessError as e:
            raise MngrError(f"git clean failed: {e.stderr}") from e

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
        result = self._host.execute_idempotent_command("git status --porcelain", cwd=path)
        if not result.success:
            raise MngrError(f"git status failed in {path}: {result.stderr}")
        return len(result.stdout.strip()) > 0

    def git_stash(self, path: Path) -> bool:
        result = self._host.execute_stateful_command(
            'git stash push -u -m "mngr-sync-stash"',
            cwd=path,
        )
        if not result.success:
            raise MngrError(f"git stash failed: {result.stderr}")
        return "No local changes to save" not in result.stdout

    def git_stash_pop(self, path: Path) -> None:
        result = self._host.execute_stateful_command("git stash pop", cwd=path)
        if not result.success:
            raise MngrError(f"git stash pop failed: {result.stderr}")

    def git_reset_hard(self, path: Path) -> None:
        result = self._host.execute_idempotent_command("git reset --hard HEAD", cwd=path)
        if not result.success:
            raise MngrError(f"git reset --hard failed: {result.stderr}")
        result = self._host.execute_idempotent_command("git clean -fd", cwd=path)
        if not result.success:
            raise MngrError(f"git clean failed: {result.stderr}")

    def get_current_branch(self, path: Path) -> str:
        result = self._host.execute_idempotent_command("git rev-parse --abbrev-ref HEAD", cwd=path)
        if not result.success:
            raise MngrError(f"Failed to get current branch: {result.stderr}")
        return result.stdout.strip()

    def is_git_repository(self, path: Path) -> bool:
        result = self._host.execute_idempotent_command("git rev-parse --git-dir", cwd=path)
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
    raise MngrError(f"Unhandled UncommittedChangesMode: {uncommitted_changes}")


@contextmanager
def stash_guard(
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
                except MngrError:
                    logger.warning(
                        "Failed to restore stashed changes after sync failure. "
                        "Run 'git stash pop' in {} to recover your changes.",
                        path,
                    )


# === Rsync Command Builders ===


@pure
def _build_rsync_command(
    source_path: Path,
    destination_path: Path,
    is_dry_run: bool,
    is_delete: bool,
) -> list[str]:
    """Build an rsync command for local-to-local file synchronization."""
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
def _build_ssh_transport_args(ssh_info: SshConnectionInfo, known_hosts_file: Path | None) -> str:
    """Build the SSH transport string for rsync -e or GIT_SSH_COMMAND."""
    _, _, port, key_path = ssh_info
    return build_ssh_transport_command(key_path, port, known_hosts_file)


@pure
def _build_ssh_git_url(ssh_info: SshConnectionInfo, remote_path: Path) -> str:
    """Build an SSH git URL from connection info and a remote path."""
    user, hostname, port, key_path = ssh_info
    return f"ssh://{user}@{hostname}:{port}{remote_path}/.git"


@pure
def _build_remote_rsync_command(
    local_path: Path,
    remote_path: Path,
    ssh_info: SshConnectionInfo,
    known_hosts_file: Path | None,
    is_push: bool,
    is_dry_run: bool,
    is_delete: bool,
) -> list[str]:
    """Build an rsync command that transfers files over SSH between local and remote.

    ``is_push`` True: local→remote (the local path is the source, remote is the destination).
    ``is_push`` False: remote→local (the remote path is the source, local is the destination).
    """
    user, hostname, _port, _key_path = ssh_info
    ssh_transport = _build_ssh_transport_args(ssh_info, known_hosts_file)

    rsync_cmd = ["rsync", "-avz", "--stats", "--exclude=.git", "-e", ssh_transport]

    if is_dry_run:
        rsync_cmd.append("--dry-run")

    if is_delete:
        rsync_cmd.append("--delete")

    if is_push:
        local_str = str(local_path)
        if not local_str.endswith("/"):
            local_str += "/"
        remote_str = str(remote_path)
        if not remote_str.endswith("/"):
            remote_str += "/"
        rsync_cmd.append(local_str)
        rsync_cmd.append(f"{user}@{hostname}:{remote_str}")
    else:
        remote_str = str(remote_path)
        if not remote_str.endswith("/"):
            remote_str += "/"
        rsync_cmd.append(f"{user}@{hostname}:{remote_str}")
        rsync_cmd.append(str(local_path))

    return rsync_cmd


def _dir_exists(host: OnlineHostInterface, path: Path) -> bool:
    """Check if a directory exists on the given host."""
    if host.is_local:
        return path.is_dir()
    result = host.execute_idempotent_command(f"test -d {shlex.quote(str(path))}")
    return result.success


def _mkdir_on_host(host: OnlineHostInterface, path: Path) -> None:
    """Idempotently create a directory on the given host."""
    if host.is_local:
        path.mkdir(parents=True, exist_ok=True)
    else:
        mkdir_result = host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(path))}")
        if not mkdir_result.success:
            raise MngrError(f"Failed to create directory {path} on host: {mkdir_result.stderr}")


# === Rsync Top-Level ===


def _do_rsync(
    local_path: Path,
    remote_host: OnlineHostInterface,
    remote_path: Path,
    is_push: bool,
    is_dry_run: bool,
    is_delete: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> RsyncResult:
    """Internal workhorse that runs the actual rsync command.

    ``is_push`` True: local→remote (the local path is the source, remote is the destination).
    ``is_push`` False: remote→local (the remote path is the source, local is the destination).
    """
    RSYNC.require()

    source_path = local_path if is_push else remote_path
    destination_path = remote_path if is_push else local_path

    # The git context lives on the destination side, which is where files may collide.
    if is_push:
        destination_git_ctx: GitContextInterface = RemoteGitContext(host=remote_host)
    else:
        destination_git_ctx = LocalGitContext(cg=cg)

    # Handle uncommitted changes in the destination. CLOBBER skips the git check
    # entirely in rsync (the destination is overwritten in place). Also skip when
    # the destination doesn't yet exist or isn't a git repo.
    if is_push:
        is_destination_exists = _dir_exists(remote_host, destination_path)
    else:
        is_destination_exists = destination_path.is_dir()
    is_destination_git_repo = (
        destination_git_ctx.is_git_repository(destination_path) if is_destination_exists else False
    )
    should_stash = uncommitted_changes != UncommittedChangesMode.CLOBBER and is_destination_git_repo

    stash_cm = (
        stash_guard(destination_git_ctx, destination_path, uncommitted_changes) if should_stash else nullcontext(False)
    )

    with stash_cm:
        # Ensure destination directory exists for subdirectory targets. Always
        # attempt mkdir (idempotent) to avoid TOCTOU race with _dir_exists.
        if is_push:
            _mkdir_on_host(remote_host, destination_path)
        else:
            destination_path.mkdir(parents=True, exist_ok=True)

        direction = "Pushing" if is_push else "Pulling"

        if remote_host.is_local:
            rsync_cmd = _build_rsync_command(source_path, destination_path, is_dry_run, is_delete)
            cmd_str = shlex.join(rsync_cmd)
            with log_span("{} files from {} to {}", direction, source_path, destination_path):
                logger.debug("Running rsync command: {}", cmd_str)
                result: CommandResult = remote_host.execute_idempotent_command(cmd_str)
            if not result.success:
                raise MngrError(f"rsync failed: {result.stderr}")
            rsync_stdout = result.stdout
        else:
            ssh_info = remote_host.get_ssh_connection_info()
            assert ssh_info is not None, "Remote host must provide SSH connection info"
            known_hosts_file = get_ssh_known_hosts_file(remote_host)
            rsync_cmd = _build_remote_rsync_command(
                local_path=local_path,
                remote_path=remote_path,
                ssh_info=ssh_info,
                known_hosts_file=known_hosts_file,
                is_push=is_push,
                is_dry_run=is_dry_run,
                is_delete=is_delete,
            )

            with log_span("{} files from {} to {} via SSH", direction, source_path, destination_path):
                logger.debug("Running rsync command: {}", shlex.join(rsync_cmd))
                try:
                    process_result = cg.run_process_to_completion(rsync_cmd)
                except ProcessError as e:
                    raise MngrError(f"rsync failed: {e.stderr}") from e

            rsync_stdout = process_result.stdout

        files_transferred, bytes_transferred = parse_rsync_output(rsync_stdout)

    logger.debug(
        "Sync complete: {} files, {} bytes transferred{}",
        files_transferred,
        bytes_transferred,
        " (dry run)" if is_dry_run else "",
    )

    return RsyncResult(
        files_transferred=files_transferred,
        bytes_transferred=bytes_transferred,
        source_path=source_path,
        destination_path=destination_path,
        is_dry_run=is_dry_run,
    )


def rsync_to_remote(
    local_path: Path,
    remote_host: OnlineHostInterface,
    remote_path: Path,
    is_dry_run: bool,
    is_delete: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> RsyncResult:
    """Rsync files from a local path to a path on ``remote_host``.

    If ``remote_host.is_local`` is True both sides are on the local machine and rsync
    runs without SSH.
    """
    return _do_rsync(
        local_path=local_path,
        remote_host=remote_host,
        remote_path=remote_path,
        is_push=True,
        is_dry_run=is_dry_run,
        is_delete=is_delete,
        uncommitted_changes=uncommitted_changes,
        cg=cg,
    )


def rsync_from_remote(
    remote_host: OnlineHostInterface,
    remote_path: Path,
    local_path: Path,
    is_dry_run: bool,
    is_delete: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> RsyncResult:
    """Rsync files from a path on ``remote_host`` to a local path.

    If ``remote_host.is_local`` is True both sides are on the local machine and rsync
    runs without SSH.
    """
    return _do_rsync(
        local_path=local_path,
        remote_host=remote_host,
        remote_path=remote_path,
        is_push=False,
        is_dry_run=is_dry_run,
        is_delete=is_delete,
        uncommitted_changes=uncommitted_changes,
        cg=cg,
    )


def rsync(
    source_host: OnlineHostInterface,
    source_path: Path,
    destination_host: OnlineHostInterface,
    destination_path: Path,
    is_dry_run: bool,
    is_delete: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> RsyncResult:
    """Generic two-endpoint rsync used by ``mngr rsync``.

    Dispatches to :func:`rsync_to_remote` or :func:`rsync_from_remote` based on
    which side is local. Rejects remote-to-remote transfers (two different
    non-local hosts) with :class:`RsyncEndpointError`.

    The CLI layer additionally rejects local-to-local; the API accepts it so internal
    callers whose agent lives on the local provider can use the same entry point.
    """
    if not source_host.is_local and not destination_host.is_local:
        raise RsyncEndpointError("mngr rsync does not support remote-to-remote transfers")
    if source_host.is_local:
        return rsync_to_remote(
            local_path=source_path,
            remote_host=destination_host,
            remote_path=destination_path,
            is_dry_run=is_dry_run,
            is_delete=is_delete,
            uncommitted_changes=uncommitted_changes,
            cg=cg,
        )
    return rsync_from_remote(
        remote_host=source_host,
        remote_path=source_path,
        local_path=destination_path,
        is_dry_run=is_dry_run,
        is_delete=is_delete,
        uncommitted_changes=uncommitted_changes,
        cg=cg,
    )


# === Git Push/Pull ===
#
# These are thin wrappers around ``git push`` / ``git pull``. They handle three
# things the user would otherwise do by hand: build the right git URL for the
# remote (ssh:// with mngr-managed key/port/known_hosts, or a bare local path),
# inject GIT_SSH_COMMAND so git uses mngr's SSH credentials, and configure the
# destination side once so a push to a checked-out branch updates the working
# tree instead of refusing. Everything else (refspecs, --force, --tags, branch
# names) is left to the caller via ``extra_args``.


def _build_git_url_and_env(
    remote_host: OnlineHostInterface,
    remote_path: Path,
) -> tuple[str, dict[str, str] | None]:
    """Build the git URL and environment to talk to ``remote_path`` on ``remote_host``.

    For local hosts the URL is the bare path (no SSH, no env). For remote hosts the
    URL is ``ssh://user@host:port/<remote_path>/.git`` and the environment carries
    ``GIT_SSH_COMMAND`` with the resolved key/port/known_hosts.
    """
    if remote_host.is_local:
        return str(remote_path), None
    ssh_info = remote_host.get_ssh_connection_info()
    assert ssh_info is not None, "Remote host must provide SSH connection info"
    known_hosts_file = get_ssh_known_hosts_file(remote_host)
    url = _build_ssh_git_url(ssh_info, remote_path)
    env = {**os.environ, "GIT_SSH_COMMAND": _build_ssh_transport_args(ssh_info, known_hosts_file)}
    return url, env


def _configure_push_destination(remote_host: OnlineHostInterface, remote_path: Path) -> None:
    """Configure the destination repo to accept a push to its checked-out branch.

    With ``receive.denyCurrentBranch=updateInstead`` git applies the push to the
    working tree on a fast-forward and refuses otherwise (instead of the default
    of always refusing). Idempotent; safe to call before every push.
    """
    result = remote_host.execute_idempotent_command(
        "git config receive.denyCurrentBranch updateInstead",
        cwd=remote_path,
    )
    if not result.success:
        raise GitSyncError(f"Failed to configure destination: {result.stderr}")


def git_push(
    local_path: Path,
    remote_host: OnlineHostInterface,
    remote_path: Path,
    extra_args: Sequence[str],
    cg: ConcurrencyGroup,
) -> None:
    """Run ``git push`` from ``local_path`` to ``remote_path`` on ``remote_host``.

    ``extra_args`` is appended verbatim to the underlying ``git push`` command
    after the constructed URL, so the caller can pass refspecs, ``--force``,
    ``--tags``, ``--dry-run``, etc.

    Raises :class:`GitSyncError` on any failure of the underlying git command.
    """
    add_safe_directory_on_remote(remote_host, remote_path)
    _configure_push_destination(remote_host, remote_path)
    url, env = _build_git_url_and_env(remote_host, remote_path)
    cmd = ["git", "-C", str(local_path), "push", url, *extra_args]
    logger.debug("Running git push: {}", shlex.join(cmd))
    try:
        cg.run_process_to_completion(cmd, env=env)
    except ProcessError as e:
        raise GitSyncError(e.stderr) from e


def git_pull(
    local_path: Path,
    remote_host: OnlineHostInterface,
    remote_path: Path,
    extra_args: Sequence[str],
    cg: ConcurrencyGroup,
) -> None:
    """Run ``git pull`` into ``local_path`` from ``remote_path`` on ``remote_host``.

    ``extra_args`` is appended verbatim to the underlying ``git pull`` command
    after the constructed URL, so the caller can pass a branch name,
    ``--rebase``, ``--ff-only``, ``--no-edit``, etc.

    Raises :class:`GitSyncError` on any failure of the underlying git command.
    """
    add_safe_directory_on_remote(remote_host, remote_path)
    url, env = _build_git_url_and_env(remote_host, remote_path)
    cmd = ["git", "-C", str(local_path), "pull", url, *extra_args]
    logger.debug("Running git pull: {}", shlex.join(cmd))
    try:
        cg.run_process_to_completion(cmd, env=env)
    except ProcessError as e:
        raise GitSyncError(e.stderr) from e
