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
import subprocess
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.enums import UpperCaseStrEnum
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


# === Gitignore status ===


class GitignoreStatus(UpperCaseStrEnum):
    """Result of checking whether a path is gitignored in a repo.

    Returned by ``check_path_gitignore_status`` /
    ``check_path_repo_gitignore_status`` so callers can build their own
    domain-specific error messages from a shared check.
    """

    # Not a git work tree, or a symlink in the path points outside the repo --
    # git won't track the path, so there is nothing to enforce.
    SKIP = auto()
    # Ignored by a gitignore rule.
    IGNORED = auto()
    # Not ignored by any rule.
    NOT_IGNORED = auto()
    # Ignored, but only via the user's global excludes (no repo-level rule
    # covers it). Returned only by ``check_path_repo_gitignore_status``; remote
    # hosts and fresh clones have no global excludes, so such a rule won't hold
    # there.
    ONLY_GLOBAL = auto()


# POSIX sh that canonicalizes ``$1`` (a path that may not fully exist yet, e.g.
# a file about to be written): it walks up to the longest existing ancestor,
# ``realpath``s that (resolving any symlink at any depth), then re-appends the
# missing remainder. Emits three lines -- resolved existing prefix, missing
# remainder (possibly empty), resolved repo root -- which the caller recombines.
# This deliberately avoids ``realpath -m`` (canonicalize-missing): GNU coreutils
# has it but macOS/BSD ``realpath`` does not, and mngr must run on both.
_RESOLVE_SYMLINKS_SH: Final[str] = (
    "p=$1; s=; "
    'while [ ! -e "$p" ] && [ "$p" != . ] && [ "$p" != / ]; do '
    "b=${p##*/}; "
    'case "$p" in */*) p=${p%/*} ;; *) p=. ;; esac; '
    "s=$b${s:+/$s}; "
    "done; "
    'realpath "$p"; printf "%s\\n" "$s"; realpath .'
)


def check_path_gitignore_status(
    host: OnlineHostInterface,
    repo_path: Path,
    relative_path: Path,
) -> tuple[GitignoreStatus, Path]:
    """Return whether ``<repo_path>/<relative_path>`` is gitignored by any rule.

    ``relative_path`` is interpreted relative to ``repo_path`` and need not
    exist yet (the common caller checks a file it is about to write). Any
    symlink along the path -- at any depth, e.g. ``.claude -> .agents`` -- is
    resolved before consulting ``git check-ignore``, which otherwise fails with
    "pathspec '...' is beyond a symbolic link". Returns a ``(status,
    checked_relative_path)`` tuple; the second element is the repo-relative path
    actually consulted (symlinks resolved), for use in caller error messages.

    Returns ``SKIP``, ``IGNORED``, or ``NOT_IGNORED`` -- never ``ONLY_GLOBAL``;
    use ``check_path_repo_gitignore_status`` when that distinction matters.
    ``SKIP`` means there is nothing to enforce (``repo_path`` is not a git work
    tree, or a symlink in the path points outside the repo). ``IGNORED`` covers
    any rule, including the user's global excludes.
    """
    checked_relative = relative_path

    is_git_repo = host.execute_idempotent_command(
        "git rev-parse --is-inside-work-tree",
        cwd=repo_path,
        timeout_seconds=5.0,
    )
    if not is_git_repo.success:
        return GitignoreStatus.SKIP, checked_relative

    # Resolve symlinks anywhere in the path so git check-ignore doesn't fail with
    # "pathspec is beyond a symbolic link". The repo root is resolved too (in
    # case repo_path itself contains symlinks) so the recombined path stays
    # repo-relative.
    resolve_result = host.execute_idempotent_command(
        f"sh -c {shlex.quote(_RESOLVE_SYMLINKS_SH)} sh {shlex.quote(str(relative_path))}",
        cwd=repo_path,
        timeout_seconds=5.0,
    )
    if resolve_result.success:
        lines = resolve_result.stdout.splitlines()
        if len(lines) == 3:
            resolved_prefix = Path(lines[0])
            remainder = Path(lines[1]) if lines[1] else Path()
            resolved_repo_root = Path(lines[2])
            try:
                checked_relative = resolved_prefix.relative_to(resolved_repo_root) / remainder
            except ValueError:
                # A symlink in the path points outside the repo -- git won't track it.
                return GitignoreStatus.SKIP, checked_relative

    result = host.execute_idempotent_command(
        f"git check-ignore -q {shlex.quote(str(checked_relative))}",
        cwd=repo_path,
        timeout_seconds=5.0,
    )
    if not result.success:
        return GitignoreStatus.NOT_IGNORED, checked_relative
    return GitignoreStatus.IGNORED, checked_relative


def check_path_repo_gitignore_status(
    host: OnlineHostInterface,
    repo_path: Path,
    relative_path: Path,
) -> tuple[GitignoreStatus, Path]:
    """Like ``check_path_gitignore_status``, but require a *repository* rule.

    A path ignored only by the user's global excludes (``core.excludesFile``)
    returns ``ONLY_GLOBAL`` rather than ``IGNORED``. Use this for preflight
    checks whose outcome must also hold on a remote host or fresh clone, which
    has no global excludes. ``SKIP`` and ``NOT_IGNORED`` pass through unchanged.
    """
    status, checked_relative = check_path_gitignore_status(host, repo_path, relative_path)
    if status is not GitignoreStatus.IGNORED:
        return status, checked_relative

    # The path is ignored by *some* rule; re-check with global excludes disabled
    # to see whether a repo-level rule covers it on its own.
    repo_only_result = host.execute_idempotent_command(
        f"git -c core.excludesFile= check-ignore -q {shlex.quote(str(checked_relative))}",
        cwd=repo_path,
        timeout_seconds=5.0,
    )
    if not repo_only_result.success:
        return GitignoreStatus.ONLY_GLOBAL, checked_relative
    return GitignoreStatus.IGNORED, checked_relative


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
    """Run ``cmd`` either via cg (captured) or with terminal-stdio passthrough.

    In the terminal-passthrough path, stdin is redirected to /dev/null so git
    can't block waiting for input it shouldn't be asking for (e.g. a credential
    prompt against an SSH-keyed remote); stdout/stderr still flow to the
    terminal so progress and error output remain visible.
    """
    if run_in_terminal:
        result = run_interactive_subprocess(cmd, stdin=subprocess.DEVNULL, env=env)
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
    user's stdout/stderr (no redirection), so progress and errors flow directly
    to the terminal -- intended for use from ``mngr git push``. stdin is
    redirected to /dev/null: any push-time prompt (credential, host-key
    confirmation, etc.) is a misconfiguration, not something the user should
    be asked to resolve interactively. Raises :class:`GitSyncError` on a
    non-zero exit either way.
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
    user's stdout/stderr (no redirection), so progress, merge-prompt text, and
    pager output flow directly to the terminal -- intended for use from
    ``mngr git pull``. stdin is redirected to /dev/null: any prompt is a
    misconfiguration we'd rather fail fast on than leave the agent hanging.
    Raises :class:`GitSyncError` on a non-zero exit either way.
    """
    add_safe_directory_on_remote(remote_host, remote_path)
    url, env = _build_git_url_and_env(remote_host, remote_path, is_push=False)
    options, positionals = _split_options_and_positionals(extra_args)
    cmd = ["git", "-C", str(local_path), "pull", *options, url, *positionals]
    logger.debug("Running git pull: {}", shlex.join(cmd))
    _run_git_command(cmd, env, cg, run_in_terminal)
