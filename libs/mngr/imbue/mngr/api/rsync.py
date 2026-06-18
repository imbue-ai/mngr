"""``mngr rsync`` API: one-shot file transfer between local and a remote host.

A thin wrapper around the ``rsync`` binary. mngr handles three things:
constructs the SSH transport for the remote endpoint, sets a few defaults
(``-avz --stats --exclude=.git``), and -- when the destination is a git
repo -- guards uncommitted changes there via ``stash_guard`` per the
caller's ``UncommittedChangesMode``. Path strings are passed through to
rsync verbatim; the caller controls trailing-slash semantics. Anything the
caller passes via ``extra_args`` is forwarded to rsync, sandwiched between
the defaults and the source/destination args.
"""

import shlex
import subprocess
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.api.git import GitContextInterface
from imbue.mngr.api.git import LocalGitContext
from imbue.mngr.api.git import RemoteGitContext
from imbue.mngr.api.git import stash_guard
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import build_ssh_transport_command
from imbue.mngr.hosts.common import get_ssh_known_hosts_file
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.deps import RSYNC
from imbue.mngr.utils.deps import SSH
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess

# (user, hostname, port, private_key_path) -- matches OnlineHostInterface.get_ssh_connection_info().
_SshConnectionInfo = tuple[str, str, int, Path]


# === Errors ===


class RsyncEndpointError(UserInputError):
    """Raised when an rsync invocation has both endpoints on the same locality (both local or both remote)."""

    user_help_text = (
        "``mngr rsync`` requires exactly one endpoint to be on the local machine and the other on a "
        "remote host. Use ``rsync`` directly for local-to-local copies."
    )


# === Command builder ===


@pure
def _build_rsync_command(
    source: str,
    destination: str,
    extra_args: Sequence[str],
    ssh_transport: str | None,
) -> list[str]:
    """Assemble the ``rsync`` argv.

    Layout is ``rsync <mngr defaults> [-e <ssh>] <caller extras> <source> <destination>``.
    Source and destination are passed verbatim, including any trailing slash the
    caller intended (rsync's "/" suffix on source means "copy contents into the
    destination" -- mngr does not impose this; the caller decides).
    """
    cmd = ["rsync", "-avz", "--stats", "--exclude=.git"]
    if ssh_transport is not None:
        cmd.extend(["-e", ssh_transport])
    cmd.extend(extra_args)
    cmd.append(source)
    cmd.append(destination)
    return cmd


# === Helpers ===


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


# === Top-level rsync ===


def _do_rsync(
    local_path: str | Path,
    remote_host: OnlineHostInterface,
    remote_path: str | Path,
    is_push: bool,
    extra_args: Sequence[str],
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
    run_in_terminal: bool = False,
) -> None:
    """Internal workhorse that runs the actual rsync command.

    ``is_push`` True: local→remote (local is source, remote is destination).
    ``is_push`` False: remote→local (remote is source, local is destination).

    Path strings are passed through verbatim. The caller is responsible for any
    trailing-slash semantics (rsync interprets a trailing slash on the source
    as "copy contents into destination" rather than "copy as a child of
    destination").

    With ``run_in_terminal=True``, ``rsync`` is run as a plain subprocess with
    the user's stdout/stderr (no redirection), so progress and errors flow
    directly to the terminal. stdin is redirected to /dev/null -- rsync
    shouldn't need to read from it, and leaving stdin connected to the
    terminal would let it consume keystrokes meant for whatever runs after.
    The function still waits for rsync to exit, so destination-side cleanup
    (stash pop on MERGE) runs as usual.

    Raises :class:`MngrError` on non-zero exit.
    """
    RSYNC.require()

    local_str = str(local_path)
    remote_str = str(remote_path)
    source_str = local_str if is_push else remote_str
    destination_str = remote_str if is_push else local_str

    # The git context lives on the destination side (where files may collide).
    # Convert to Path for the git operations -- trailing slashes are irrelevant
    # for git's view of the directory.
    destination_for_git = Path(remote_str if is_push else local_str)
    if is_push:
        destination_git_ctx: GitContextInterface = RemoteGitContext(host=remote_host)
    else:
        destination_git_ctx = LocalGitContext(cg=cg)

    # CLOBBER skips the git check entirely. Also skip when the destination
    # doesn't yet exist or isn't a git repo.
    if is_push:
        is_destination_exists = _dir_exists(remote_host, destination_for_git)
    else:
        is_destination_exists = destination_for_git.is_dir()
    is_destination_git_repo = (
        destination_git_ctx.is_git_repository(destination_for_git) if is_destination_exists else False
    )
    should_stash = uncommitted_changes != UncommittedChangesMode.CLOBBER and is_destination_git_repo

    stash_cm = (
        stash_guard(destination_git_ctx, destination_for_git, uncommitted_changes)
        if should_stash
        else nullcontext(False)
    )

    with stash_cm:
        # Ensure destination directory exists for subdirectory targets. Always
        # attempt mkdir (idempotent) to avoid TOCTOU race with _dir_exists.
        if is_push:
            _mkdir_on_host(remote_host, destination_for_git)
        else:
            destination_for_git.mkdir(parents=True, exist_ok=True)

        direction = "Pushing" if is_push else "Pulling"

        if remote_host.is_local:
            rsync_cmd = _build_rsync_command(source_str, destination_str, extra_args, ssh_transport=None)
        else:
            # rsync to a remote host uses the ssh binary as its transport (-e ssh);
            # ssh is optional, so surface a clear error if it's absent.
            SSH.require()
            ssh_info = remote_host.get_ssh_connection_info()
            assert ssh_info is not None, "Remote host must provide SSH connection info"
            user, hostname, port, key_path = ssh_info
            ssh_transport = build_ssh_transport_command(key_path, port, get_ssh_known_hosts_file(remote_host))
            remote_uri = f"{user}@{hostname}:{remote_str}"
            ssh_source = local_str if is_push else remote_uri
            ssh_destination = remote_uri if is_push else local_str
            rsync_cmd = _build_rsync_command(ssh_source, ssh_destination, extra_args, ssh_transport=ssh_transport)

        if run_in_terminal:
            with log_span("{} files from {} to {}", direction, source_str, destination_str):
                logger.debug("Running rsync command: {}", shlex.join(rsync_cmd))
                terminal_result = run_interactive_subprocess(rsync_cmd, stdin=subprocess.DEVNULL)
            if terminal_result.returncode != 0:
                raise MngrError(f"rsync exited with status {terminal_result.returncode}")
        elif remote_host.is_local:
            cmd_str = shlex.join(rsync_cmd)
            with log_span("{} files from {} to {}", direction, source_str, destination_str):
                logger.debug("Running rsync command: {}", cmd_str)
                result = remote_host.execute_idempotent_command(cmd_str)
            if not result.success:
                raise MngrError(f"rsync failed: {result.stderr}")
        else:
            with log_span("{} files from {} to {} via SSH", direction, source_str, destination_str):
                logger.debug("Running rsync command: {}", shlex.join(rsync_cmd))
                try:
                    cg.run_process_to_completion(rsync_cmd)
                except ProcessError as e:
                    raise MngrError(f"rsync failed: {e.stderr}") from e


def rsync_to_remote(
    local_path: str | Path,
    remote_host: OnlineHostInterface,
    remote_path: str | Path,
    extra_args: Sequence[str],
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
    run_in_terminal: bool = False,
) -> None:
    """Rsync files from a local path to a path on ``remote_host``.

    If ``remote_host.is_local`` is True both sides are on the local machine and
    rsync runs without SSH. Path strings are passed to rsync verbatim -- include
    a trailing slash on ``local_path`` if you want "copy contents into
    destination" semantics rather than "copy as a child of destination".

    See :func:`_do_rsync` for ``run_in_terminal`` semantics.
    """
    _do_rsync(
        local_path=local_path,
        remote_host=remote_host,
        remote_path=remote_path,
        is_push=True,
        extra_args=extra_args,
        uncommitted_changes=uncommitted_changes,
        cg=cg,
        run_in_terminal=run_in_terminal,
    )


def rsync_from_remote(
    remote_host: OnlineHostInterface,
    remote_path: str | Path,
    local_path: str | Path,
    extra_args: Sequence[str],
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
    run_in_terminal: bool = False,
) -> None:
    """Rsync files from a path on ``remote_host`` to a local path.

    If ``remote_host.is_local`` is True both sides are on the local machine and
    rsync runs without SSH. Path strings are passed to rsync verbatim -- include
    a trailing slash on ``remote_path`` if you want "copy contents into
    destination" semantics rather than "copy as a child of destination".

    See :func:`_do_rsync` for ``run_in_terminal`` semantics.
    """
    _do_rsync(
        local_path=local_path,
        remote_host=remote_host,
        remote_path=remote_path,
        is_push=False,
        extra_args=extra_args,
        uncommitted_changes=uncommitted_changes,
        cg=cg,
        run_in_terminal=run_in_terminal,
    )


def rsync(
    source_host: OnlineHostInterface,
    source_path: str | Path,
    destination_host: OnlineHostInterface,
    destination_path: str | Path,
    extra_args: Sequence[str],
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
    run_in_terminal: bool = False,
) -> None:
    """Generic two-endpoint rsync used by ``mngr rsync``.

    Dispatches to :func:`rsync_to_remote` or :func:`rsync_from_remote` based on
    which side is local. Rejects remote-to-remote transfers (two different
    non-local hosts) with :class:`RsyncEndpointError`.

    The CLI layer additionally rejects local-to-local; the API accepts it so internal
    callers whose agent lives on the local provider can use the same entry point.

    See :func:`_do_rsync` for ``run_in_terminal`` semantics.
    """
    if not source_host.is_local and not destination_host.is_local:
        raise RsyncEndpointError("mngr rsync does not support remote-to-remote transfers")
    if source_host.is_local:
        rsync_to_remote(
            local_path=source_path,
            remote_host=destination_host,
            remote_path=destination_path,
            extra_args=extra_args,
            uncommitted_changes=uncommitted_changes,
            cg=cg,
            run_in_terminal=run_in_terminal,
        )
    else:
        rsync_from_remote(
            remote_host=source_host,
            remote_path=source_path,
            local_path=destination_path,
            extra_args=extra_args,
            uncommitted_changes=uncommitted_changes,
            cg=cg,
            run_in_terminal=run_in_terminal,
        )
