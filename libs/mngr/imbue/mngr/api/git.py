"""Git push/pull wrappers and the shared stash-guard / git-context machinery.

The push/pull functions are thin pass-through wrappers around ``git push`` and
``git pull``: mngr resolves the host, builds the right URL (ssh:// with
mngr-managed credentials, or a bare local path), and configures the
destination's ``receive.denyCurrentBranch`` once before handing off to git.
Everything else (refspecs, ``--force``, ``--rebase``, ``--no-edit``, etc.) is
forwarded verbatim through ``extra_args``.

This module also owns the stash-guard machinery used by ``mngr_pair`` and by
``api/rsync.py``: ``GitContextInterface`` + ``LocalGitContext`` /
``RemoteGitContext`` abstract over "run a git command here or on a host", and
``stash_guard`` is a context manager that handles uncommitted-changes modes
around a sync operation.
"""

import os
import shlex
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import add_safe_directory_on_remote
from imbue.mngr.hosts.common import build_ssh_transport_command
from imbue.mngr.hosts.common import get_ssh_known_hosts_file
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.deps import SSH
from imbue.mngr.utils.git_utils import get_current_branch
from imbue.mngr.utils.git_utils import is_git_repository
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess

# (user, hostname, port, private_key_path) -- matches OnlineHostInterface.get_ssh_connection_info().
_SshConnectionInfo = tuple[str, str, int, Path]


# === Errors ===


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


# === Git context (run git here, or on a remote host) ===


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


# === Uncommitted-changes handling ===


def _handle_uncommitted_changes(
    git_ctx: GitContextInterface,
    path: Path,
    uncommitted_changes: UncommittedChangesMode,
) -> bool:
    """Apply ``uncommitted_changes`` mode at ``path``; return True if a stash was taken."""
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
    """Stash uncommitted changes at ``path`` for the body of the with-block.

    Yields True if changes were stashed. On normal exit, pops the stash when mode
    is MERGE. On exception, also attempts a pop for MERGE mode and logs a warning
    if it fails (so the user can recover via ``git stash pop``).
    """
    did_stash = _handle_uncommitted_changes(git_ctx, path, uncommitted_changes)
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


# === SSH transport helpers ===


@pure
def _build_ssh_transport_args(ssh_info: _SshConnectionInfo, known_hosts_file: Path | None) -> str:
    """SSH transport string for GIT_SSH_COMMAND (and, for rsync, the ``-e`` arg)."""
    _, _, port, key_path = ssh_info
    return build_ssh_transport_command(key_path, port, known_hosts_file)


@pure
def _build_ssh_git_url(ssh_info: _SshConnectionInfo, remote_path: Path) -> str:
    """Build an SSH git URL from connection info and a remote path."""
    user, hostname, port, _key_path = ssh_info
    return f"ssh://{user}@{hostname}:{port}{remote_path}/.git"


def _build_git_url_and_env(
    remote_host: OnlineHostInterface,
    remote_path: Path,
    *,
    is_push: bool,
) -> tuple[str, dict[str, str] | None]:
    """Build the git URL and environment to talk to ``remote_path`` on ``remote_host``.

    For local hosts the URL is the bare path (no SSH, no env). For remote hosts the
    URL is ``ssh://user@host:port/<remote_path>/.git`` and the environment carries
    ``GIT_SSH_COMMAND`` with the resolved key/port/known_hosts. For push only, also
    sets ``GIT_LFS_SKIP_PUSH=1`` so an LFS-tracked file doesn't try to upload to a
    remote LFS server that the agent's repo isn't configured for.
    """
    if remote_host.is_local:
        return str(remote_path), {**os.environ, "GIT_LFS_SKIP_PUSH": "1"} if is_push else None
    # Talking to a remote host means git shells out to the ssh binary (via
    # GIT_SSH_COMMAND); ssh is optional, so surface a clear error if it's absent.
    SSH.require()
    ssh_info = remote_host.get_ssh_connection_info()
    assert ssh_info is not None, "Remote host must provide SSH connection info"
    known_hosts_file = get_ssh_known_hosts_file(remote_host)
    url = _build_ssh_git_url(ssh_info, remote_path)
    env: dict[str, str] = {**os.environ, "GIT_SSH_COMMAND": _build_ssh_transport_args(ssh_info, known_hosts_file)}
    if is_push:
        env["GIT_LFS_SKIP_PUSH"] = "1"
    return url, env


# === Push/pull command assembly ===


def _split_options_and_positionals(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split git args into options (those starting with ``-``) and positionals.

    git's command line is ``git <cmd> [<options>] [<repo> [<refspec>...]]``: options
    must come before the repository, and anything after the repository is treated
    as a refspec (for push) or forwarded to the underlying ``git fetch`` (for pull).
    Since mngr supplies the URL itself, every flag the caller passes needs to land
    before that URL, regardless of where they typed it. Args starting with ``-`` go
    to options; everything else goes to positionals. Options that take a
    separate-token value (e.g. ``-o foo``) must use the ``--opt=foo`` form so the
    value doesn't get reclassified as a positional.
    """
    options: list[str] = []
    positionals: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            options.append(arg)
        else:
            positionals.append(arg)
    return options, positionals


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


def _default_push_refspec(
    local_path: Path,
    remote_host: OnlineHostInterface,
    remote_path: Path,
    cg: ConcurrencyGroup,
) -> str:
    """Return ``<local_current_branch>:<remote_current_branch>`` for the no-args default."""
    local_branch = get_current_branch(local_path, cg)
    result = remote_host.execute_idempotent_command("git symbolic-ref --short HEAD", cwd=remote_path)
    if not result.success:
        raise GitSyncError(f"Failed to detect remote current branch: {result.stderr}")
    return f"{local_branch}:{result.stdout.strip()}"


def _run_git_command(cmd: list[str], env: dict[str, str] | None, cg: ConcurrencyGroup, run_in_terminal: bool) -> None:
    """Run ``cmd`` either via cg (captured) or with terminal-stdio passthrough."""
    if run_in_terminal:
        result = run_interactive_subprocess(cmd, env=env)
        if result.returncode != 0:
            raise GitSyncError(f"git exited with status {result.returncode}")
        return
    try:
        cg.run_process_to_completion(cmd, env=env)
    except ProcessError as e:
        raise GitSyncError(e.stderr) from e


def git_push(
    local_path: Path,
    remote_host: OnlineHostInterface,
    remote_path: Path,
    extra_args: Sequence[str],
    cg: ConcurrencyGroup,
    run_in_terminal: bool = False,
) -> None:
    """Run ``git push`` from ``local_path`` to ``remote_path`` on ``remote_host``.

    ``extra_args`` is passed through to the underlying ``git push`` command. Args
    starting with ``-`` (up to the first non-option) go before the constructed URL
    so they're parsed as options; the rest go after the URL as refspecs. If the
    caller supplies no refspec, mngr defaults to
    ``<local_current_branch>:<remote_current_branch>`` so ``mngr git push my-agent``
    works on mngr's worktree-style agents (where the agent has its own branch).

    With ``run_in_terminal=True``, ``git`` is run as a plain subprocess with the
    user's stdin/stdout/stderr (no redirection), so progress and errors flow
    directly to the terminal -- intended for use from ``mngr git push``.
    Raises :class:`GitSyncError` on a non-zero exit either way.
    """
    add_safe_directory_on_remote(remote_host, remote_path)
    _configure_push_destination(remote_host, remote_path)
    url, env = _build_git_url_and_env(remote_host, remote_path, is_push=True)
    options, positionals = _split_options_and_positionals(extra_args)
    if not positionals:
        positionals = [_default_push_refspec(local_path, remote_host, remote_path, cg)]
    cmd = ["git", "-C", str(local_path), "push", *options, url, *positionals]
    logger.debug("Running git push: {}", shlex.join(cmd))
    _run_git_command(cmd, env, cg, run_in_terminal)


def git_pull(
    local_path: Path,
    remote_host: OnlineHostInterface,
    remote_path: Path,
    extra_args: Sequence[str],
    cg: ConcurrencyGroup,
    run_in_terminal: bool = False,
) -> None:
    """Run ``git pull`` into ``local_path`` from ``remote_path`` on ``remote_host``.

    ``extra_args`` is passed through to the underlying ``git pull`` command. Args
    starting with ``-`` (up to the first non-option) go before the constructed URL
    so they're parsed as options; the rest go after the URL as refspecs.

    With ``run_in_terminal=True``, ``git`` is run as a plain subprocess with the
    user's stdin/stdout/stderr (no redirection), so merge prompts and pager
    output flow directly to the terminal -- intended for use from
    ``mngr git pull``. Raises :class:`GitSyncError` on a non-zero exit either way.
    """
    add_safe_directory_on_remote(remote_host, remote_path)
    url, env = _build_git_url_and_env(remote_host, remote_path, is_push=False)
    options, positionals = _split_options_and_positionals(extra_args)
    cmd = ["git", "-C", str(local_path), "pull", *options, url, *positionals]
    logger.debug("Running git pull: {}", shlex.join(cmd))
    _run_git_command(cmd, env, cg, run_in_terminal)
