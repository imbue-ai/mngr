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

Status is fully derived from disk + the live resolver; there is no
state.json. For each in-flight destroy ``<paths.data_dir>/destroying/<agent_id>/``
contains three files: ``pid`` (single-line text), ``host_id`` (the host the
destroy is tearing down) and ``output.log`` (combined stdout+stderr from the
bash wrapper). :py:class:`DestroyingStatus` is computed from ``pid`` liveness +
whether the workspace's *host* is still up -- the caller answers that via
``is_host_still_active`` (the workspace agent still in
``MngrCliBackendResolver.list_active_workspace_ids()`` OR its host not yet in a
terminal ``DESTROYED`` state). Keying on the host, not just the workspace
agent, is deliberate: a minds host also runs a ``system-services`` agent, so a
destroy that removed only the workspace agent must read as FAILED, not DONE.

  - dir present + pid alive                       -> RUNNING
  - dir present + pid dead + host gone            -> DONE   (caller deletes the dir)
  - dir present + pid dead + host still up        -> FAILED (kept for inspection)

The ~1-second window between the destroy subprocess exiting and the
``mngr observe`` discovery tail picking up the host's ``DESTROYED`` state
can briefly flip status to FAILED for a successful destroy. The detail
page poll picks up the corrected status on the next tick. Acceptable
jitter; documented in ``specs/detached-destroy-flow/spec.md``.
"""

import os
import shutil
import subprocess
from collections.abc import Callable
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
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_DESTROYING_DIR_NAME: Final[str] = "destroying"
_PID_FILE_NAME: Final[str] = "pid"
_LOG_FILE_NAME: Final[str] = "output.log"
_HOST_ID_FILE_NAME: Final[str] = "host_id"


class DestroyingStatus(UpperCaseStrEnum):
    """Status of a detached destroy subprocess.

    Values are derived from disk + resolver state -- callers don't write
    them anywhere; :py:func:`read_destroying` computes them per request.
    """

    RUNNING = auto()
    DONE = auto()
    FAILED = auto()


class DestroyingRecord(FrozenModel):
    """Snapshot of a detached destroy's state.

    All fields are derived from disk inspection of
    ``<paths.data_dir>/destroying/<agent_id>/`` plus the caller's
    ``is_host_still_active`` answer; there is no on-disk state.json.
    """

    agent_id: AgentId = Field(description="Agent that is being / was being destroyed")
    pid: int = Field(description="PID of the detached bash wrapper that runs `mngr destroy`")
    started_at: datetime = Field(description="Wall-clock time the destroy was started (directory mtime)")
    pid_alive: bool = Field(description="Whether the wrapper PID is still live")
    is_host_still_active: bool = Field(
        description=(
            "Whether the workspace's host is still up: the workspace agent is still in "
            "list_active_workspace_ids(), or its host has not yet reached DESTROYED. A destroy "
            "is only DONE once this is False (the whole host, not just the agent, is gone)."
        )
    )
    status: DestroyingStatus = Field(description="Derived status; see DestroyingStatus docstring")
    log_path: Path = Field(description="Absolute path to output.log for the detail page tail")


def _destroying_dir(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return paths.data_dir / _DESTROYING_DIR_NAME / str(agent_id)


def _pid_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _PID_FILE_NAME


def _log_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _LOG_FILE_NAME


def _host_id_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _HOST_ID_FILE_NAME


def read_host_id(agent_id: AgentId, paths: WorkspacePaths) -> HostId | None:
    """Return the host id recorded for this agent's destroy, or None if absent/unreadable.

    Written by :func:`start_destroy` so a later status read can ask the
    resolver whether that *host* (not just the workspace agent) is actually
    gone before declaring the destroy DONE.
    """
    path = _host_id_file(paths, agent_id)
    if not path.is_file():
        return None
    try:
        value = path.read_text().strip()
    except OSError as e:
        logger.warning("Could not read host_id file {} for destroying agent {}: {}", path, agent_id, e)
        return None
    return HostId(value) if value else None


def _is_pid_alive(pid: int) -> bool:
    """Best-effort check whether ``pid`` is still running.

    Three cases to handle:

    - Pid was never our child (we're a fresh minds backend after the
      original Popen-parent died). ``os.kill(pid, 0)`` is the right
      check: ``ProcessLookupError`` => dead, ok => alive.
    - Pid IS our child and is still running. Same -- ``os.kill(pid, 0)``
      succeeds, and we want to report alive.
    - Pid IS our child and exited but hasn't been reaped (zombie).
      ``os.kill(pid, 0)`` succeeds because the pid still occupies the
      process table, but the destroy is done. We need
      ``os.waitpid(pid, WNOHANG)`` to reap it; once reaped, the next
      ``os.kill(pid, 0)`` will correctly raise ``ProcessLookupError``.

    PermissionError is reported as alive (kept-alive default for the
    not-our-pid edge case where someone else's pid happens to match).
    """
    try:
        # Reap if we're the parent and the child has finished. ECHILD
        # ("not our child") fires on the post-restart case; that's fine,
        # the os.kill below handles the actual liveness check there.
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError as e:
        logger.trace("waitpid({}) raised {}; falling through to kill(0)", pid, e)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _build_destroy_command(host_id: HostId, mngr_binary: str = MNGR_BINARY) -> list[str]:
    """Build the bash command run by the detached subprocess.

    Always fans out to *every* agent on the host (the workspace agent plus the
    constant ``system-services`` agent that every minds workspace runs in the
    same container). Destroying only the workspace agent would leave
    system-services -- and therefore the host and its cloud instance -- alive,
    so there is deliberately no single-agent path here: a minds workspace
    teardown is a *host* teardown.

    Lease release is not chained explicitly because ``mngr destroy`` handles
    it: when the last agent on a host is destroyed, ``mngr destroy`` calls
    ``provider.destroy_host`` which (for ``imbue_cloud``) wipes the on-VPS data
    and releases the lease back to the pool, and (for the VPS providers)
    terminates the instance. The destroyed-host grace period
    (``destroyed_host_persisted_seconds``) then only retains historical state.
    The same chain runs again if ``mngr delete`` is called later by GC; it's
    idempotent on an already-released lease.
    """
    # ``mngr list ... --ids`` writes one id per line; ``mngr destroy -f -`` reads
    # ids from stdin. The pipe handles host-mates fanout in one shot.
    shell_command = f"{mngr_binary} list --include 'host.id == \"{host_id}\"' --ids | {mngr_binary} destroy -f -"
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
    wrapper's PID is written to ``pid`` and the host id to ``host_id`` (so a
    later status read can confirm the *host* is gone, not just the agent).

    Idempotent: if a destroy is already running for this agent (``pid`` exists
    and is alive), we return the existing record without spawning a second
    process.

    ``mngr_binary`` defaults to the absolute path resolved at import time
    (so the packaged app finds mngr in its venv even when Electron's PATH
    doesn't include the venv bin dir). Tests override this with ``"mngr"``
    so a PATH-prepended fake mngr binary can be picked up.
    """
    # ``is_host_still_active=True`` is conservative: we only reuse a RUNNING
    # record (pid alive), and pid-alive derives RUNNING regardless of this flag.
    existing = read_destroying(agent_id, paths, is_host_still_active=True)
    if existing is not None and existing.status == DestroyingStatus.RUNNING:
        logger.info("Destroy for {} already running (pid={}); reusing", agent_id, existing.pid)
        return existing

    dir_path = _destroying_dir(paths, agent_id)
    dir_path.mkdir(parents=True, exist_ok=True)
    log_path = _log_file(paths, agent_id)
    pid_path = _pid_file(paths, agent_id)

    # Record the host id up front so status reads can ask the resolver whether
    # the host (not just the workspace agent) actually went away.
    _host_id_file(paths, agent_id).write_text(f"{host_id}\n")

    # Truncate the log file so a Retry doesn't show the previous run's output.
    log_path.write_bytes(b"")

    command = _build_destroy_command(host_id, mngr_binary=mngr_binary)
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
        is_host_still_active=True,
        status=DestroyingStatus.RUNNING,
        log_path=log_path,
    )


def read_destroying(
    agent_id: AgentId,
    paths: WorkspacePaths,
    is_host_still_active: bool,
) -> DestroyingRecord | None:
    """Read the on-disk record for a single agent's destroy, or None if no dir.

    ``is_host_still_active`` is supplied by the caller (which owns the resolver)
    rather than fetched here, so this module stays free of the resolver's
    threading + locking shape. It must be True when *either* the workspace
    agent is still in ``list_active_workspace_ids()`` *or* the workspace's host
    has not yet reached DESTROYED -- so a destroy that tore down only the
    workspace agent while system-services kept the host alive reads as FAILED,
    not DONE. The status table:

      - dir absent                                              -> None
      - dir present, pid alive                                  -> RUNNING
      - dir present, pid dead, is_host_still_active=False       -> DONE
      - dir present, pid dead, is_host_still_active=True        -> FAILED

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
    pid_alive = _is_pid_alive(pid)
    if pid_alive:
        status = DestroyingStatus.RUNNING
    elif is_host_still_active:
        status = DestroyingStatus.FAILED
    else:
        status = DestroyingStatus.DONE
    started_at = datetime.fromtimestamp(dir_path.stat().st_mtime, tz=timezone.utc)
    return DestroyingRecord(
        agent_id=agent_id,
        pid=pid,
        started_at=started_at,
        pid_alive=pid_alive,
        is_host_still_active=is_host_still_active,
        status=status,
        log_path=_log_file(paths, agent_id),
    )


def list_destroying(
    paths: WorkspacePaths,
    is_host_still_active: Callable[[AgentId], bool],
) -> dict[AgentId, DestroyingRecord]:
    """Walk ``<paths.data_dir>/destroying/`` and return a record per agent_id.

    Used by the landing-page renderer. ``is_host_still_active`` answers, per
    agent, whether that workspace's host is still up (see
    :func:`read_destroying`); the caller closes it over the current discovery
    snapshot so the same view is shared across every record's status derivation.
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
        record = read_destroying(agent_id, paths, is_host_still_active=is_host_still_active(agent_id))
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
