"""Detached discard of a dead (interrupted / failed) pending create attempt.

Discarding an interrupted create attempt may have to destroy a leftover half-built
Lima / Docker host, which takes tens of seconds -- so, exactly like a
workspace destroy (see :mod:`destroying`), the ``mngr destroy`` runs as a
detached subprocess that survives a desktop-client exit, with its combined
output streamed to the create attempt detail page. Unlike a workspace destroy there
is no discovery state to consult: the wrapper records the destroy's exit code
on disk, and the status is derived purely from pid liveness + that exit code.

For each in-flight discard ``<paths.data_dir>/discarding_create_attempts/<create_attempt_id>/``
holds ``pid`` (absent for a no-host discard), ``output.log`` and ``exit_code``
(written by the wrapper when the destroy finishes):

  - dir present + pid alive                    -> RUNNING
  - dir present + pid dead + exit_code == 0    -> DONE   (caller deletes the
    pending record and this dir)
  - dir present + pid dead + exit_code != 0    -> FAILED (kept for inspection;
    the discard can be retried)

The pending record itself is deleted only on DONE -- a failed destroy keeps
the row visible with the error, per the reliability decision log.
"""

import os
import shlex
import shutil
import subprocess
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.destroying import is_pid_alive

_DISCARDING_DIR_NAME: Final[str] = "discarding_create_attempts"
_PID_FILE_NAME: Final[str] = "pid"
_LOG_FILE_NAME: Final[str] = "output.log"
_EXIT_CODE_FILE_NAME: Final[str] = "exit_code"


class CreateAttemptDiscardStatus(UpperCaseStrEnum):
    """Status of a detached create attempt discard, derived per read from disk."""

    RUNNING = auto()
    DONE = auto()
    FAILED = auto()


class CreateAttemptDiscardRecord(FrozenModel):
    """Snapshot of a detached create attempt discard's state."""

    create_attempt_id: str = Field(description="Pending create attempt being discarded")
    status: CreateAttemptDiscardStatus = Field(description="Derived status; see the module docstring's table")
    started_at: datetime = Field(description="Wall-clock time the discard was started (directory mtime)")
    log_path: Path = Field(description="Absolute path to output.log for the detail-page tail")


def _discard_dir(paths: WorkspacePaths, create_attempt_id: str) -> Path:
    return paths.data_dir / _DISCARDING_DIR_NAME / create_attempt_id


def start_discard_of_host(
    create_attempt_id: str,
    paths: WorkspacePaths,
    host_id: str,
    provider_name: str,
    env: dict[str, str] | None = None,
    mngr_binary: str = MNGR_BINARY,
) -> CreateAttemptDiscardRecord:
    """Spawn the detached subprocess that destroys the create attempt's leftover host.

    ``--force`` makes a host that vanished between the lookup and the destroy
    report-and-skip (exit 0), so the discard is idempotent. The wrapper writes
    the destroy's exit code to ``exit_code`` as its last act; the pending
    record is deleted by the caller only once a status read reports DONE.

    Idempotent: an already-RUNNING discard for this create attempt is returned
    without spawning a second subprocess.
    """
    existing = read_discard(create_attempt_id, paths)
    if existing is not None and existing.status is CreateAttemptDiscardStatus.RUNNING:
        logger.info("Discard for create attempt {} already running; reusing", create_attempt_id)
        return existing

    dir_path = _discard_dir(paths, create_attempt_id)
    dir_path.mkdir(parents=True, exist_ok=True)
    log_path = dir_path / _LOG_FILE_NAME
    # Truncate leftovers so a retry does not show the previous run's output,
    # and clear a stale exit_code so the fresh run reads RUNNING.
    log_path.write_bytes(b"")
    (dir_path / _EXIT_CODE_FILE_NAME).unlink(missing_ok=True)

    # ``host_id`` / ``provider_name`` come from mngr's own JSON listing (no
    # untrusted input), and every substitution is shlex-quoted anyway.
    shell_command = "{} destroy {} --force; echo $? > {}".format(
        shlex.quote(mngr_binary),
        shlex.quote(f"@{host_id}.{provider_name}"),
        shlex.quote(str(dir_path / _EXIT_CODE_FILE_NAME)),
    )
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            ["bash", "-c", shell_command],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=dict(os.environ) if env is None else dict(env),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()
    (dir_path / _PID_FILE_NAME).write_text(f"{process.pid}\n")
    logger.info(
        "Started detached discard for create attempt {} (pid={}, host={}, provider={})",
        create_attempt_id,
        process.pid,
        host_id,
        provider_name,
    )
    return CreateAttemptDiscardRecord(
        create_attempt_id=create_attempt_id,
        status=CreateAttemptDiscardStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
        log_path=log_path,
    )


def start_discard_without_host(
    create_attempt_id: str, paths: WorkspacePaths, message: str
) -> CreateAttemptDiscardRecord:
    """Record an immediately-DONE discard for a create attempt with no leftover host.

    Keeps the one uniform flow on the detail page (stream the log, poll the
    status, navigate home on DONE) whether or not a host had to be destroyed.
    """
    dir_path = _discard_dir(paths, create_attempt_id)
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / _PID_FILE_NAME).unlink(missing_ok=True)
    log_path = dir_path / _LOG_FILE_NAME
    log_path.write_text(message + "\n")
    (dir_path / _EXIT_CODE_FILE_NAME).write_text("0\n")
    return CreateAttemptDiscardRecord(
        create_attempt_id=create_attempt_id,
        status=CreateAttemptDiscardStatus.DONE,
        started_at=datetime.now(timezone.utc),
        log_path=log_path,
    )


def read_discard(create_attempt_id: str, paths: WorkspacePaths) -> CreateAttemptDiscardRecord | None:
    """Read the on-disk record for one create attempt's discard, or None if no dir."""
    dir_path = _discard_dir(paths, create_attempt_id)
    if not dir_path.is_dir():
        return None
    pid_path = dir_path / _PID_FILE_NAME
    pid: int | None = None
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError) as e:
            logger.warning("Could not parse discard pid file {}: {}", pid_path, e)
    if pid is not None and is_pid_alive(pid):
        status = CreateAttemptDiscardStatus.RUNNING
    else:
        exit_code_path = dir_path / _EXIT_CODE_FILE_NAME
        exit_code_text = ""
        if exit_code_path.is_file():
            try:
                exit_code_text = exit_code_path.read_text().strip()
            except OSError as e:
                logger.warning("Could not read discard exit-code file {}: {}", exit_code_path, e)
        if exit_code_text == "0":
            status = CreateAttemptDiscardStatus.DONE
        elif exit_code_text:
            status = CreateAttemptDiscardStatus.FAILED
        elif pid is None:
            # Neither a pid nor an exit code: the spawn never got as far as
            # writing either (or the dir is mid-create-attempt). Treat as RUNNING so
            # a racing status read does not flash FAILED.
            status = CreateAttemptDiscardStatus.RUNNING
        else:
            # The wrapper died without writing its exit code (killed mid-run).
            status = CreateAttemptDiscardStatus.FAILED
    started_at = datetime.fromtimestamp(dir_path.stat().st_mtime, tz=timezone.utc)
    return CreateAttemptDiscardRecord(
        create_attempt_id=create_attempt_id,
        status=status,
        started_at=started_at,
        log_path=dir_path / _LOG_FILE_NAME,
    )


def delete_discard(create_attempt_id: str, paths: WorkspacePaths) -> bool:
    """Remove the discard dir. Idempotent; errors are logged and swallowed."""
    dir_path = _discard_dir(paths, create_attempt_id)
    if not dir_path.exists():
        return False
    try:
        shutil.rmtree(dir_path)
    except OSError as e:
        logger.warning("Could not remove discard dir {}: {}", dir_path, e)
        return False
    return True


def read_discard_log_chunk(create_attempt_id: str, paths: WorkspacePaths, offset: int) -> tuple[bytes, int]:
    """Read ``output.log`` from ``offset`` to current EOF.

    Returns ``(content_bytes, next_offset)``; empty bytes when there is no new
    content. Raises ``FileNotFoundError`` if the log file is missing.
    """
    log_path = _discard_dir(paths, create_attempt_id) / _LOG_FILE_NAME
    if not log_path.is_file():
        raise FileNotFoundError(log_path)
    file_size = log_path.stat().st_size
    if offset >= file_size:
        return b"", file_size
    with log_path.open("rb") as f:
        f.seek(offset)
        content = f.read(file_size - offset)
    return content, offset + len(content)
