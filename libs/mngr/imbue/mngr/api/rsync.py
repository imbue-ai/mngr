"""``mngr rsync`` API: one-shot file transfer between local and a remote host.

Wraps the ``rsync`` binary. mngr supplies the trailing slash on the source
(to get "copy contents" semantics), passes ``-avz --stats --exclude=.git``, and
arranges the SSH transport when the remote isn't local. Before transferring,
if the destination is a git repo, ``stash_guard`` protects any uncommitted
changes there per the caller's ``UncommittedChangesMode``.
"""

import shlex
from contextlib import nullcontext
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
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
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr.utils.deps import RSYNC
from imbue.mngr.utils.rsync_utils import parse_rsync_output

# (user, hostname, port, private_key_path) -- matches OnlineHostInterface.get_ssh_connection_info().
_SshConnectionInfo = tuple[str, str, int, Path]


# === Errors and result ===


class RsyncEndpointError(UserInputError):
    """Raised when an rsync invocation has both endpoints on the same locality (both local or both remote)."""

    user_help_text = (
        "``mngr rsync`` requires exactly one endpoint to be on the local machine and the other on a "
        "remote host. Use ``rsync`` directly for local-to-local copies."
    )


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


# === Command builders ===


@pure
def _build_rsync_command(
    source_path: Path,
    destination_path: Path,
    is_dry_run: bool,
    is_delete: bool,
) -> list[str]:
    """Build an rsync command for local-to-local file synchronization.

    The trailing slash on the source path makes rsync copy the *contents* of the
    source into the destination, rather than copying the source directory itself
    as a child of the destination. The destination side doesn't need a trailing
    slash -- rsync ignores it there.
    """
    rsync_cmd = ["rsync", "-avz", "--stats", "--exclude=.git"]

    if is_dry_run:
        rsync_cmd.append("--dry-run")

    if is_delete:
        rsync_cmd.append("--delete")

    source_str = str(source_path)
    if not source_str.endswith("/"):
        source_str += "/"

    rsync_cmd.append(source_str)
    rsync_cmd.append(str(destination_path))

    return rsync_cmd


@pure
def _build_remote_rsync_command(
    local_path: Path,
    remote_path: Path,
    ssh_info: _SshConnectionInfo,
    known_hosts_file: Path | None,
    is_push: bool,
    is_dry_run: bool,
    is_delete: bool,
) -> list[str]:
    """Build an rsync command that transfers files over SSH between local and remote.

    ``is_push`` True: local→remote (the local path is the source, remote is the destination).
    ``is_push`` False: remote→local (the remote path is the source, local is the destination).

    Only the source path gets a trailing slash; rsync ignores trailing slashes on
    the destination.
    """
    user, hostname, port, key_path = ssh_info
    ssh_transport = build_ssh_transport_command(key_path, port, known_hosts_file)

    rsync_cmd = ["rsync", "-avz", "--stats", "--exclude=.git", "-e", ssh_transport]

    if is_dry_run:
        rsync_cmd.append("--dry-run")

    if is_delete:
        rsync_cmd.append("--delete")

    if is_push:
        local_str = str(local_path)
        if not local_str.endswith("/"):
            local_str += "/"
        rsync_cmd.append(local_str)
        rsync_cmd.append(f"{user}@{hostname}:{remote_path}")
    else:
        remote_str = str(remote_path)
        if not remote_str.endswith("/"):
            remote_str += "/"
        rsync_cmd.append(f"{user}@{hostname}:{remote_str}")
        rsync_cmd.append(str(local_path))

    return rsync_cmd


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
