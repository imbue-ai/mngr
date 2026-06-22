"""Per-agent destroy lifecycle, run as a detached subprocess.

Why this file uses raw ``subprocess.Popen`` (with the matching ratchet
exclusion in ``test_ratchets.py``): we need the destroy command to
*outlive* the minds desktop client. ``mngr destroy`` against a Docker
host can take ~30-60 seconds; if minds shuts down (Electron quit,
laptop close, crash) mid-destroy, we want the destroy to keep going to
completion rather than leak a half-destroyed agent. ``ConcurrencyGroup``
guarantees the opposite -- every spawned process is killed on group
exit -- so it is structurally the wrong tool here. Same justification as
``apps/minds/imbue/minds/desktop_client/latchkey/_spawn.py``.

A minds destroy is a whole-*host* teardown: the host also runs a
``system-services`` agent, so the wrapper fans out over every agent on the
host (see :func:`_build_destroy_command`) -- destroying only the workspace
agent would leave the host (and its cloud instance) alive.

Status is derived from disk; there is no state.json. For each in-flight
destroy ``<paths.data_dir>/destroying/<agent_id>/`` holds:

  - ``pid`` (single-line int): the detached bash wrapper's PID.
  - ``process_start`` (single-line float): the wrapper's psutil
    ``create_time()``, so a recycled PID is not mistaken for a live wrapper.
  - ``output.log``: combined stdout+stderr from the wrapper.
  - ``result`` (single-line int): the wrapper's exit code, written
    atomically the instant ``mngr destroy`` finishes. Its presence is the
    authoritative completion signal.

:py:class:`DestroyingStatus` is computed as:

  - ``result`` present, exit code 0        -> DONE   (caller finalizes the record)
  - ``result`` present, exit code non-zero -> FAILED (kept for inspection)
  - ``result`` absent, pid alive           -> RUNNING
  - ``result`` absent, pid dead            -> FAILED (wrapper died mid-destroy)

Reading the wrapper's own recorded exit code -- rather than inferring the
host's state from the lagging ``mngr observe`` discovery cache -- keeps the
status correct the instant the wrapper exits and across a minds restart:
no spurious FAILED while discovery catches up, and a genuinely failed
destroy is never mistaken for DONE (which would orphan a still-billing host).
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

import psutil
from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_DESTROYING_DIR_NAME: Final[str] = "destroying"
_PID_FILE_NAME: Final[str] = "pid"
_LOG_FILE_NAME: Final[str] = "output.log"
_RESULT_FILE_NAME: Final[str] = "result"
_RESULT_TMP_SUFFIX: Final[str] = ".partial"
_PROCESS_START_FILE_NAME: Final[str] = "process_start"

# Two different processes cannot occupy one PID within a second of each
# other (the slot stays held until the first is reaped), so a create_time
# gap this large means the PID was recycled.
_CREATE_TIME_TOLERANCE_SECONDS: Final[float] = 1.0


class DestroyingStatus(UpperCaseStrEnum):
    """Status of a detached destroy subprocess.

    Values are derived from disk state -- callers don't write them
    anywhere; :py:func:`read_destroying` computes them per request.
    """

    RUNNING = auto()
    DONE = auto()
    FAILED = auto()


class DestroyingRecord(FrozenModel):
    """Snapshot of a detached destroy's state.

    All fields are derived from disk inspection of
    ``<paths.data_dir>/destroying/<agent_id>/``; there is no on-disk
    state.json.
    """

    agent_id: AgentId = Field(description="Agent that is being / was being destroyed")
    pid: int = Field(description="PID of the detached bash wrapper that runs `mngr destroy`")
    started_at: datetime = Field(description="Wall-clock time the destroy was started (directory mtime)")
    pid_alive: bool = Field(description="Whether the wrapper PID is still live")
    exit_code: int | None = Field(
        default=None,
        description="Exit code the wrapper recorded on completion, or None while it is still running",
    )
    status: DestroyingStatus = Field(description="Derived status; see DestroyingStatus docstring")
    log_path: Path = Field(description="Absolute path to output.log for the detail page tail")


def _destroying_dir(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return paths.data_dir / _DESTROYING_DIR_NAME / str(agent_id)


def _pid_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _PID_FILE_NAME


def _log_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _LOG_FILE_NAME


def _result_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _RESULT_FILE_NAME


def _process_start_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _PROCESS_START_FILE_NAME


def _process_create_time(pid: int) -> float | None:
    """Return the process creation time for ``pid``, or None if it is already gone."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _is_pid_alive(pid: int, expected_create_time: float | None = None) -> bool:
    """Whether ``pid`` still names the destroy wrapper we spawned.

    ``expected_create_time`` (the wrapper's ``create_time()`` recorded at
    spawn) guards against PID reuse: if the OS recycled the PID onto an
    unrelated process while minds was closed, the live process's
    create_time will not match and we report the wrapper as gone rather
    than mistaking the stranger for a still-running destroy.

    Reaps our own finished child first so it stops occupying the table;
    ``ECHILD`` ("not our child") fires after a minds restart re-parents
    the wrapper to init, which is fine.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError as e:
        logger.trace("waitpid({}) raised {}; falling through to psutil check", pid, e)
    try:
        proc = psutil.Process(pid)
        create_time = proc.create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
    if expected_create_time is not None and abs(create_time - expected_create_time) > _CREATE_TIME_TOLERANCE_SECONDS:
        return False
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _read_result(paths: WorkspacePaths, agent_id: AgentId) -> int | None:
    """Read the wrapper's recorded exit code, or None if it has not finished."""
    try:
        text = _result_file(paths, agent_id).read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        logger.warning("Unparseable result file for destroying agent {}: {!r}", agent_id, text)
        return None


def _read_process_start(paths: WorkspacePaths, agent_id: AgentId) -> float | None:
    """Read the wrapper's recorded create_time, or None if not recorded."""
    try:
        text = _process_start_file(paths, agent_id).read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _build_destroy_command(host_id: HostId, result_path: Path, mngr_binary: str = MNGR_BINARY) -> list[str]:
    """Build the bash command run by the detached subprocess.

    Always fans out to *every* agent on the host (the workspace agent plus the
    constant ``system-services`` agent that every minds workspace runs in the
    same container). Destroying only the workspace agent would leave
    system-services -- and therefore the host and its cloud instance -- alive,
    so there is deliberately no single-agent path here: a minds workspace
    teardown is a *host* teardown.

    The wrapper records ``mngr destroy``'s exit code to ``result_path``
    atomically (write-then-rename) once it finishes; this is the authoritative
    completion signal :func:`read_destroying` derives status from. ``set -o
    pipefail`` ensures a failed ``mngr list`` surfaces as a non-zero result
    rather than being masked by the trailing ``mngr destroy`` exiting 0 on
    empty input.

    Lease release is not chained explicitly because ``mngr destroy`` handles
    it: when the last agent on a host is destroyed, ``mngr destroy`` calls
    ``provider.destroy_host`` which (for ``imbue_cloud``) wipes the on-VPS data
    and releases the lease back to the pool, and (for the VPS providers)
    terminates the instance.
    """
    # ``mngr list ... --ids`` writes one id per line; ``mngr destroy -f -`` reads
    # ids from stdin. The pipe handles host-mates fanout in one shot.
    destroy = f"{mngr_binary} list --include 'host.id == \"{host_id}\"' --ids | {mngr_binary} destroy -f -"
    final = shlex.quote(str(result_path))
    partial = shlex.quote(str(result_path) + _RESULT_TMP_SUFFIX)
    shell_command = (
        "set -o pipefail\n"
        + destroy
        + "\n"
        + "rc=$?\n"
        + "printf '%s\\n' \"$rc\" > "
        + partial
        + " && mv "
        + partial
        + " "
        + final
        + "\n"
        + "exit $rc\n"
    )
    return ["bash", "-c", shell_command]


def start_destroy(
    agent_id: AgentId,
    paths: WorkspacePaths,
    host_id: HostId,
    env: dict[str, str] | None = None,
    mngr_binary: str = MNGR_BINARY,
) -> DestroyingRecord:
    """Spawn the detached destroy subprocess that tears down ``host_id``.

    The caller (the desktop-client API handler) resolves ``host_id`` from the
    in-memory backend resolver -- which always knows it for a workspace the
    user can see -- and must refuse to destroy when it can't, rather than
    passing a sentinel. ``host_id`` is required: there is no single-agent
    fallback (see :func:`_build_destroy_command`).

    The subprocess is detached (``start_new_session=True``), so it survives a
    minds-backend exit. stdout+stderr go to a single ``output.log`` file; the
    wrapper's PID is written to ``pid`` and its create_time to ``process_start``
    (so a later status read can reject a recycled PID), and the wrapper itself
    records its exit code to ``result`` on completion.

    Idempotent: if a destroy is already running for this agent (``pid`` exists
    and is alive), we return the existing record without spawning a second
    process.

    ``mngr_binary`` defaults to the absolute path resolved at import time
    (so the packaged app finds mngr in its venv even when Electron's PATH
    doesn't include the venv bin dir). Tests override this with ``"mngr"``
    so a PATH-prepended fake mngr binary can be picked up.
    """
    existing = read_destroying(agent_id, paths)
    if existing is not None and existing.status == DestroyingStatus.RUNNING:
        logger.info("Destroy for {} already running (pid={}); reusing", agent_id, existing.pid)
        return existing

    dir_path = _destroying_dir(paths, agent_id)
    dir_path.mkdir(parents=True, exist_ok=True)
    log_path = _log_file(paths, agent_id)
    pid_path = _pid_file(paths, agent_id)
    result_path = _result_file(paths, agent_id)
    process_start_path = _process_start_file(paths, agent_id)

    # Clear the prior run's artifacts so a Retry starts clean: a stale
    # ``result`` would read as an immediate terminal status, a stale
    # ``process_start`` would point at the previous wrapper, and the log
    # would show the old attempt's output.
    log_path.write_bytes(b"")
    result_path.unlink(missing_ok=True)
    process_start_path.unlink(missing_ok=True)

    command = _build_destroy_command(host_id, result_path, mngr_binary=mngr_binary)
    log_handle = log_path.open("ab")
    try:
        process_env = dict(os.environ) if env is None else dict(env)
        # bash -c with a command string we built from a host_id resolved from
        # discovery (no untrusted input). The S603 ruff rule is not in our
        # select list; intent is documented for future readers.
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=process_env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()

    pid_path.write_text(f"{process.pid}\n")
    create_time = _process_create_time(process.pid)
    if create_time is not None:
        process_start_path.write_text(f"{create_time!r}\n")
    started_at = datetime.now(timezone.utc)
    logger.info(
        "Started detached destroy for agent {} (pid={}, host_id={}, log={})",
        agent_id,
        process.pid,
        host_id,
        log_path,
    )
    return DestroyingRecord(
        agent_id=agent_id,
        pid=process.pid,
        started_at=started_at,
        pid_alive=True,
        exit_code=None,
        status=DestroyingStatus.RUNNING,
        log_path=log_path,
    )


def read_destroying(
    agent_id: AgentId,
    paths: WorkspacePaths,
) -> DestroyingRecord | None:
    """Read the on-disk record for a single agent's destroy, or None if no dir.

    Status is derived from the wrapper's recorded ``result`` (exit code)
    first, falling back to pid liveness only while no result has been
    recorded yet:

      - dir absent                            -> None
      - result present, exit code 0           -> DONE
      - result present, exit code non-zero    -> FAILED
      - result absent, pid alive              -> RUNNING
      - result absent, pid dead               -> FAILED

    Returns ``None`` for the absent case; otherwise a populated record.
    """
    dir_path = _destroying_dir(paths, agent_id)
    pid_path = _pid_file(paths, agent_id)
    if not dir_path.is_dir() or not pid_path.is_file():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError) as e:
        logger.warning("Could not parse pid file {} for destroying agent {}: {}", pid_path, agent_id, e)
        return None
    exit_code = _read_result(paths, agent_id)
    pid_alive = _is_pid_alive(pid, _read_process_start(paths, agent_id))
    if exit_code is not None:
        status = DestroyingStatus.DONE if exit_code == 0 else DestroyingStatus.FAILED
    elif pid_alive:
        status = DestroyingStatus.RUNNING
    else:
        status = DestroyingStatus.FAILED
    started_at = datetime.fromtimestamp(dir_path.stat().st_mtime, tz=timezone.utc)
    return DestroyingRecord(
        agent_id=agent_id,
        pid=pid,
        started_at=started_at,
        pid_alive=pid_alive,
        exit_code=exit_code,
        status=status,
        log_path=_log_file(paths, agent_id),
    )


def list_destroying(paths: WorkspacePaths) -> dict[AgentId, DestroyingRecord]:
    """Walk ``<paths.data_dir>/destroying/`` and return a record per agent_id.

    Used by the landing-page renderer. Status no longer depends on the
    resolver snapshot -- each record's status comes from disk alone.
    """
    root = paths.data_dir / _DESTROYING_DIR_NAME
    if not root.is_dir():
        return {}
    records: dict[AgentId, DestroyingRecord] = {}
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            agent_id = AgentId(entry.name)
        except ValueError:
            logger.warning("Skipping destroying entry with non-AgentId name: {}", entry.name)
            continue
        record = read_destroying(agent_id, paths)
        if record is not None:
            records[agent_id] = record
    return records


def delete_destroying(agent_id: AgentId, paths: WorkspacePaths) -> bool:
    """Remove ``<paths.data_dir>/destroying/<agent_id>/``. Idempotent.

    Returns ``True`` if the directory was present and removed,
    ``False`` if there was nothing to remove. Best-effort: errors during
    rmtree are logged and swallowed so a half-deleted dir doesn't break
    the next render.
    """
    dir_path = _destroying_dir(paths, agent_id)
    if not dir_path.exists():
        return False
    try:
        shutil.rmtree(dir_path)
    except OSError as e:
        logger.warning("Could not remove destroying dir {}: {}", dir_path, e)
        return False
    return True


def read_log_chunk(agent_id: AgentId, paths: WorkspacePaths, offset: int) -> tuple[bytes, int]:
    """Read ``output.log`` from ``offset`` to current EOF.

    Returns ``(content_bytes, next_offset)``. Empty bytes when there is
    no new content. Raises ``FileNotFoundError`` if the log file is
    missing (caller should return 404).
    """
    log_path = _log_file(paths, agent_id)
    if not log_path.is_file():
        raise FileNotFoundError(log_path)
    file_size = log_path.stat().st_size
    if offset >= file_size:
        return b"", file_size
    with log_path.open("rb") as f:
        f.seek(offset)
        content = f.read(file_size - offset)
    return content, offset + len(content)
