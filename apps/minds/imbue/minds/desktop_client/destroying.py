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
contains exactly two files: ``pid`` (single-line text) and
``output.log`` (combined stdout+stderr from the bash wrapper).
:py:class:`DestroyingStatus` is computed from ``pid`` liveness +
whether ``agent_id`` still appears in
``MngrCliBackendResolver.list_known_workspace_ids()``:

  - dir present + pid alive                  -> RUNNING
  - dir present + pid dead + agent gone      -> DONE   (caller deletes the dir)
  - dir present + pid dead + agent still up  -> FAILED (kept for inspection)

The ~1-second window between the destroy subprocess exiting and the
``mngr observe`` discovery tail picking up the ``AgentDestroyed`` event
can briefly flip status to FAILED for a successful destroy. The detail
page poll picks up the corrected status on the next tick. Acceptable
jitter; documented in ``specs/detached-destroy-flow/spec.md``.
"""

import json
import os
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
from imbue.mngr.primitives import AgentId

_DESTROYING_DIR_NAME: Final[str] = "destroying"
_PID_FILE_NAME: Final[str] = "pid"
_LOG_FILE_NAME: Final[str] = "output.log"


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
    ``agent_in_resolver`` answer; there is no on-disk state.json.
    """

    agent_id: AgentId = Field(description="Agent that is being / was being destroyed")
    pid: int = Field(description="PID of the detached bash wrapper that runs `mngr destroy`")
    started_at: datetime = Field(description="Wall-clock time the destroy was started (directory mtime)")
    pid_alive: bool = Field(description="Whether the wrapper PID is still live")
    agent_in_resolver: bool = Field(
        description="Whether the agent is still listed in MngrCliBackendResolver.list_known_workspace_ids()"
    )
    status: DestroyingStatus = Field(description="Derived status; see DestroyingStatus docstring")
    log_path: Path = Field(description="Absolute path to output.log for the detail page tail")


def _destroying_dir(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return paths.data_dir / _DESTROYING_DIR_NAME / str(agent_id)


def _pid_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _PID_FILE_NAME


def _log_file(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    return _destroying_dir(paths, agent_id) / _LOG_FILE_NAME


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


def lookup_host_id(agent_id: AgentId, env: dict[str, str] | None = None, timeout_seconds: float = 10.0) -> str | None:
    """Look up the host_id for ``agent_id`` via ``mngr list --include 'id == "..."' --format json``.

    Used by the API handler to compute host_id *synchronously* before
    spawning the detached destroy, so the spawned bash wrapper can do
    host-mates fanout without a second ``mngr list`` round-trip.

    Returns ``None`` on any failure (mngr exit non-zero, malformed JSON,
    no agents matched). Callers fall back to single-agent destroy in
    that case.
    """
    process_env = dict(os.environ) if env is None else dict(env)
    # Fixed mngr binary + a filter expression we built from a validated AgentId,
    # no untrusted input on argv -- ruff would flag the bare subprocess.run via
    # S603 if it were in our select list (it's not).
    try:
        result = subprocess.run(
            [
                MNGR_BINARY,
                "list",
                "--include",
                f'id == "{agent_id}"',
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            env=process_env,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("lookup_host_id({}) failed: {}", agent_id, e)
        return None
    if result.returncode != 0:
        logger.debug(
            "lookup_host_id({}) returned exit {}: {}",
            agent_id,
            result.returncode,
            result.stderr.strip()[:200],
        )
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.warning("lookup_host_id({}) returned non-JSON: {}", agent_id, e)
        return None
    agents = data.get("agents") if isinstance(data, dict) else None
    if not agents:
        return None
    host = agents[0].get("host", {}) if isinstance(agents[0], dict) else {}
    host_id = host.get("id") if isinstance(host, dict) else None
    return host_id if isinstance(host_id, str) and host_id else None


def _build_destroy_command(agent_id: AgentId, host_id: str | None, mngr_binary: str = MNGR_BINARY) -> list[str]:
    """Build the bash command run by the detached subprocess.

    With a host_id, fans out to every agent on the same host (matches the
    pre-detached behavior in ``AgentCreator._destroy_all_agents_on_host``).
    Without a host_id (lookup failed before spawn), falls back to
    single-agent destroy.

    Lease release is intentionally NOT chained here. For imbue_cloud
    agents the lease lifecycle is owned by
    ``mngr_imbue_cloud.instance.delete_host``, called by mngr's GC after
    the destroyed-host grace period. Eagerly chaining
    ``mngr imbue_cloud hosts release`` from minds was duplicating that
    responsibility in two places.
    """
    if host_id is not None:
        # ``mngr list ... --ids`` writes one id per line; ``mngr destroy -f -`` reads
        # ids from stdin. The pipe handles host-mates fanout in one shot.
        shell_command = f"{mngr_binary} list --include 'host.id == \"{host_id}\"' --ids | {mngr_binary} destroy -f -"
    else:
        shell_command = f"{mngr_binary} destroy {agent_id} -f"
    return ["bash", "-c", shell_command]


def start_destroy(
    agent_id: AgentId,
    paths: WorkspacePaths,
    host_id: str | None,
    env: dict[str, str] | None = None,
) -> DestroyingRecord:
    """Spawn the detached destroy subprocess.

    Caller (the desktop-client API handler) is expected to have already
    looked up ``host_id`` via ``mngr list`` -- if that lookup failed,
    pass ``None`` and we fall back to a single-agent destroy.

    The subprocess is detached (``start_new_session=True``), so it
    survives a minds-backend exit. stdout+stderr go to a single
    ``output.log`` file; the wrapper's PID is written to ``pid``.

    Idempotent: if a destroy is already running for this agent
    (``pid`` exists and is alive), we return the existing record
    without spawning a second process.
    """
    existing = read_destroying(agent_id, paths, agent_in_resolver=True)
    if existing is not None and existing.status == DestroyingStatus.RUNNING:
        logger.info("Destroy for {} already running (pid={}); reusing", agent_id, existing.pid)
        return existing

    dir_path = _destroying_dir(paths, agent_id)
    dir_path.mkdir(parents=True, exist_ok=True)
    log_path = _log_file(paths, agent_id)
    pid_path = _pid_file(paths, agent_id)

    # Truncate the log file so a Retry doesn't show the previous run's output.
    log_path.write_bytes(b"")

    command = _build_destroy_command(agent_id, host_id)
    log_handle = log_path.open("ab")
    try:
        process_env = dict(os.environ) if env is None else dict(env)
        # bash -c with a command string we built from a validated AgentId +
        # the host_id we just looked up via mngr list. The S603 ruff rule is
        # not in our select list; intent is documented for future readers.
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
        agent_in_resolver=True,
        status=DestroyingStatus.RUNNING,
        log_path=log_path,
    )


def read_destroying(
    agent_id: AgentId,
    paths: WorkspacePaths,
    agent_in_resolver: bool,
) -> DestroyingRecord | None:
    """Read the on-disk record for a single agent's destroy, or None if no dir.

    ``agent_in_resolver`` is supplied by the caller (typically
    ``agent_id in MngrCliBackendResolver.list_known_workspace_ids()``)
    rather than fetched here so this module stays free of the resolver's
    threading + locking shape. The status table:

      - dir absent                                            -> None
      - dir present, pid alive                                -> RUNNING
      - dir present, pid dead, agent_in_resolver=False        -> DONE
      - dir present, pid dead, agent_in_resolver=True         -> FAILED

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
    elif agent_in_resolver:
        status = DestroyingStatus.FAILED
    else:
        status = DestroyingStatus.DONE
    started_at = datetime.fromtimestamp(dir_path.stat().st_mtime, tz=timezone.utc)
    return DestroyingRecord(
        agent_id=agent_id,
        pid=pid,
        started_at=started_at,
        pid_alive=pid_alive,
        agent_in_resolver=agent_in_resolver,
        status=status,
        log_path=_log_file(paths, agent_id),
    )


def list_destroying(
    paths: WorkspacePaths,
    agent_ids_in_resolver: frozenset[AgentId],
) -> dict[AgentId, DestroyingRecord]:
    """Walk ``<paths.data_dir>/destroying/`` and return a record per agent_id.

    Used by the landing-page renderer. ``agent_ids_in_resolver`` is the
    snapshot of ``MngrCliBackendResolver.list_known_workspace_ids()`` at
    render time so the same set is shared across every record's status
    derivation.
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
        record = read_destroying(agent_id, paths, agent_in_resolver=agent_id in agent_ids_in_resolver)
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
