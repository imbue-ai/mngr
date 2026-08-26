"""Collect the bug report's workspace archive via the template's resident collector.

The workspace template ships a collector script inside every workspace
(``WORKSPACE_COLLECTOR_PATH``). It gathers the workspace logs and the recent
conversations the report asked for, secret-scans everything as plaintext with
the template's own scan gate, and prints exactly one line: the base64 of a zip.
Anything requested that the collector withheld is a plain-words line in the
archive's own ``collection-notes.txt`` member, so the archive explains itself
and this module has nothing to interpret -- it decodes the line and stages the
file. The exec rides ``mngr exec --format json``, whose envelope separates the
remote stdout from mngr's own output.

The workspace's latchkey gateway (remote workspaces run one on their outer
host) logs outside the container, so the resident collector cannot see it. A
second, cheap ``mngr exec --outer`` mirrors its tail into the latchkey plugin
data dir, where the existing latchkey attachment groups sweep it onto every
event -- see ``refresh_remote_gateway_log_mirror``.

The Electron shell's console tail is not this module's concern: it is the
shell's own output, staged app-side by ``report_collector`` without any
workspace involvement.

Everything here degrades rather than fails. A stopped host, an unreachable
container, or a slow exec resolves to a one-line ``note`` on the returned
result -- a plain-words sentence recorded on the report -- and a bug report is
never blocked by its own diagnostics, so ``collect_workspace_diagnostics``
raises nothing for a collection failure.

The staged archive is attached one-shot, by exact path, to the report it was
collected for (``submit_manual_bug_report``'s ``report_file_paths``). Each
report stages into its own fresh directory under the system temp dir, so
collections that overlap -- reports submit immediately and upload in the
background -- cannot see each other's files, nothing ever needs deleting up
front, and no Sentry attachment group can sweep a staged file onto an
unrelated event. Leftover staging is the operating system's ordinary temp
cleanup's to reclaim.
"""

import base64
import os
import tempfile
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.mngr_command import extract_exec_failure_detail
from imbue.minds.desktop_client.mngr_command import extract_exec_stdout
from imbue.minds.desktop_client.mngr_command import run_mngr_capturing
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr_latchkey.remote_gateway import REMOTE_GATEWAY_LOG_FILENAME
from imbue.mngr_latchkey.remote_gateway import REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote_gateway import REMOTE_TUNNEL_LOG_FILENAME

# Where the workspace template installs the resident collector. The contract
# with default-workspace-template's ``system/scripts/collect_bug_report_diagnostics.py``
# is one line of base64 on stdout, decoding to the archive.
WORKSPACE_COLLECTOR_PATH: Final[str] = "/home/user/workspace/system/scripts/collect_bug_report_diagnostics.py"

# Whole-collection budget. Passed to ``mngr exec --timeout`` (capping the
# in-container work) and used as the outer subprocess timeout (capping the
# provider round-trip on top of it), so a wedged workspace cannot stretch a bug
# report's collection beyond this. The collector carries its own, shorter
# scanner timeout so a slow secret scan degrades to a withheld-content note
# instead of blowing the whole budget.
#
# Nobody waits on this: reports are filed immediately and collect on a
# background strand, and a workspace that is simply down is short-circuited by
# the host-stopped check rather than by this. So the budget only has to exceed a
# healthy collection, and the cost of it being too SMALL is much worse than it
# being too large. The collector's mngr calls are scoped to the local provider
# precisely to keep a healthy collection far inside this.
WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS: Final[float] = 300.0

# The slice of the collection budget the in-container secret scan may spend, so
# a slow scan degrades to withheld content instead of timing out the whole
# exec. A fraction rather than a constant: when a test runs collection under a
# longer budget (a shared CI sandbox is several times slower than the user
# machines the production budget is policy for), the scanner's share must
# stretch with it, or the scan starves while the exec idles.
_SCAN_TIMEOUT_FRACTION_OF_BUDGET: Final[float] = 0.6

# The staged archive's filename inside a report's own staging directory (a
# fresh temp dir per report, so concurrent reports cannot collide and nothing
# needs sweeping). The ``.zip`` suffix is a contract with the upload path,
# which reads it to know the archive is already compressed.
STAGED_ZIP_FILENAME: Final[str] = "workspace.zip"

# Where collection mirrors the remote gateway tail, inside the latchkey plugin
# data dir: the existing ``latchkey_raw_logs`` Sentry attachment group sweeps
# every ``*.log`` there onto every event, so landing the tail in that dir IS
# the whole attachment mechanism -- no staging, keys, or reservations.
REMOTE_GATEWAY_TAIL_MIRROR_FILENAME: Final[str] = "remote_gateway_tail.log"

# How much of each remote gateway log's end the latchkey tail reads: enough for
# the requests around the bug, small enough to never dominate the report.
LATCHKEY_LOG_TAIL_BYTES: Final[int] = 256 * 1024

# How recently a gateway log must have been written to for its tail to ride: a
# gateway nothing has touched in over a day describes some earlier session, not
# the one the bug was filed from. In minutes because it is handed to
# ``find -mmin``.
LATCHKEY_LOG_MAX_AGE_MINUTES: Final[int] = 24 * 60

# How much of a failure's quoted diagnosis the note may carry -- the remote
# command's stderr, a failed-agent error, or the exec's own stderr tail: enough
# for the verdict line, never enough to bloat the report document.
_NOTE_STDERR_MAX_CHARS: Final[int] = 300

# Host states in which the container is definitively not running, so an ``mngr
# exec --no-start`` could only wait out its timeout. All of them short-circuit
# to the same note: the user-visible fact is that there was no running
# workspace to read, not which flavour of not-running it was.
_NOT_RUNNING_HOST_STATES: Final[frozenset[HostState]] = frozenset(
    {
        HostState.STOPPING,
        HostState.STOPPED,
        HostState.PAUSED,
        HostState.CRASHED,
        HostState.FAILED,
        HostState.DESTROYED,
    }
)


class WorkspaceCollectionResult(FrozenModel):
    """What one workspace collection produced.

    Exactly one of the two fields is set: the staged archive when collection
    landed (whatever the collector withheld is explained inside it, in its
    ``collection-notes.txt`` member), or a one-line plain-words note saying why
    there is no archive at all.
    """

    staged_zip_path: Path | None = Field(
        default=None, description="The staged workspace archive, written under the report's staging dir."
    )
    note: str | None = Field(
        default=None, description="Why there is no archive, in plain words; None when one was staged."
    )

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}


def make_staging_dir() -> Path:
    """A fresh private staging directory for one report's files.

    Under the system temp dir, so concurrent reports cannot collide, nothing is
    ever deleted up front, and leftover staging (a crash between staging and
    upload) is the operating system's ordinary temp cleanup's to reclaim rather
    than a sweep of ours.
    """
    return Path(tempfile.mkdtemp(prefix="minds-bug-report-"))


def resolve_workspace_host_state(
    backend_resolver: BackendResolverInterface, workspace_agent_id: AgentId
) -> HostState | None:
    """Read a workspace's host state from the passive discovery snapshot.

    Passive only -- no synchronous ``mngr list`` -- so asking costs nothing. None
    means discovery has not surfaced the workspace or its host, which is not
    evidence that it is down, so collection is attempted anyway.
    """
    info = backend_resolver.get_agent_display_info(workspace_agent_id)
    if info is None:
        return None
    return backend_resolver.get_host_state(HostId(info.host_id))


def build_diagnostics_argv(
    workspace_agent_id: AgentId,
    include_logs: bool,
    include_transcript: bool,
    timeout_seconds: float = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS,
) -> list[str]:
    """The ``mngr exec`` argv that runs the resident collector inside the workspace.

    ``--format json`` wraps the remote stdout in mngr's own envelope, so the
    collector's one base64 line comes back clean of any of mngr's output.
    ``--no-start`` keeps a bug report from booting a stopped workspace just by
    asking it for logs. ``timeout_seconds`` defaults to the production budget;
    only tests pass anything else (a shared CI sandbox is several times slower
    than the user machines the budget is policy for).
    """
    flags = []
    if include_logs:
        flags.append("--logs")
    if include_transcript:
        flags.append("--transcript")
    flags.append(f"--scan-timeout={int(timeout_seconds * _SCAN_TIMEOUT_FRACTION_OF_BUDGET)}")
    command = f"python3 {WORKSPACE_COLLECTOR_PATH} {' '.join(flags)}"
    return [
        MNGR_BINARY,
        "exec",
        str(workspace_agent_id),
        command,
        "--format",
        "json",
        "--timeout",
        str(int(timeout_seconds)),
        "--no-start",
        "--quiet",
    ]


def build_latchkey_logs_argv(
    workspace_agent_id: AgentId,
    timeout_seconds: float = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS,
) -> list[str]:
    """The ``mngr exec --outer`` argv that tails the workspace outer host's gateway logs.

    ``--outer`` targets the machine hosting the container -- where a remote
    workspace's latchkey gateway runs and logs -- and ``--missing-outer ignore``
    skips cleanly when no outer host is reachable. ``--format json`` separates
    the remote stdout from mngr's own output. ``find -mmin`` keeps the tail to
    logs written to in the last day (a silent gateway describes some earlier
    session), and ``tail -v`` labels each file's section even for a single
    match (GNU-only, and the VPS hosts this runs against are Linux; a host
    without GNU tail has no resident gateway). The trailing ``true`` keeps a
    fruitless ``find`` from reading as a failed command: an empty stdout is the
    observation "nothing to tail", and it must arrive as a success.
    """
    latchkey_dir = f'"$HOME"/{REMOTE_LATCHKEY_DIR_NAME}'
    command = (
        f"find {latchkey_dir} -maxdepth 1 "
        f"\\( -name {REMOTE_GATEWAY_LOG_FILENAME} -o -name {REMOTE_TUNNEL_LOG_FILENAME} \\) "
        f"-mmin -{LATCHKEY_LOG_MAX_AGE_MINUTES} "
        f"-exec tail -v -c {LATCHKEY_LOG_TAIL_BYTES} {{}} + 2>/dev/null; true"
    )
    return [
        MNGR_BINARY,
        "exec",
        str(workspace_agent_id),
        command,
        "--outer",
        "--missing-outer",
        "ignore",
        "--format",
        "json",
        "--timeout",
        str(int(timeout_seconds)),
        "--no-start",
        "--quiet",
    ]


def refresh_remote_gateway_log_mirror(
    workspace_agent_id: AgentId,
    *,
    latchkey_plugin_data_dir: Path,
    concurrency_group: ConcurrencyGroup,
    env: dict[str, str],
    timeout_seconds: float,
) -> None:
    """Mirror the outer host's latchkey gateway tail into the latchkey plugin data dir.

    The existing ``latchkey_raw_logs`` Sentry attachment group sweeps every
    ``*.log`` in that dir onto every event, so writing the tail there is the
    entire attachment mechanism -- the same standing as the desktop gateway's
    own logs, which already ride every report unscanned. A fresh observation of
    "nothing to tail" removes the mirror (a stale tail must not impersonate a
    current one); an exec that could not observe anything -- no reachable outer
    host, a failed run -- leaves whatever mirror exists. Never raises: the
    mirror is best-effort diagnostics.
    """
    mirror_path = latchkey_plugin_data_dir / REMOTE_GATEWAY_TAIL_MIRROR_FILENAME
    argv = build_latchkey_logs_argv(workspace_agent_id, timeout_seconds)
    try:
        stdout, _returncode, _stderr = run_mngr_capturing(
            concurrency_group, argv, env, timeout_seconds=timeout_seconds
        )
    except MngrCommandError as exc:
        logger.info("The latchkey gateway tail for {} could not be refreshed: {}", workspace_agent_id, exc)
        return
    # ``--missing-outer ignore`` leaves the envelope's outer_results empty when
    # the workspace has no reachable outer host: nothing was observed.
    tail_text = extract_exec_stdout(stdout, results_key="outer_results")
    if tail_text is None:
        return
    tail_text = tail_text.strip()
    try:
        if not tail_text:
            mirror_path.unlink(missing_ok=True)
            return
        latchkey_plugin_data_dir.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(tail_text + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write the latchkey gateway tail mirror at {}: {}", mirror_path, exc)


def collect_workspace_diagnostics(
    workspace_agent_id: AgentId,
    *,
    include_logs: bool,
    include_transcript: bool,
    staging_dir: Path,
    host_state: HostState | None,
    concurrency_group: ConcurrencyGroup,
    timeout_seconds: float = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS,
    latchkey_plugin_data_dir: Path | None = None,
) -> WorkspaceCollectionResult:
    """Stage the workspace archive for one report, or say in one line why not.

    Runs the template's resident collector over ``mngr exec`` with the flags
    for the boxes the user ticked and stages the archive it prints back under
    ``staging_dir`` -- the report's own private directory (see
    ``make_staging_dir``). Whatever the collector withheld is explained inside
    the archive itself; this function only reports the outcomes where there is
    no archive at all.

    ``latchkey_plugin_data_dir`` (when latchkey is configured) receives a
    mirror of the outer host's gateway-log tail via a second, cheap ``mngr exec
    --outer`` -- see ``refresh_remote_gateway_log_mirror``; the existing
    latchkey attachment groups sweep it from there, so it needs no staging or
    bookkeeping here.

    The ``mngr`` subprocesses inherit this process's environment -- bootstrap
    owns ``MNGR_HOST_DIR`` -- exactly like every other desktop-client exec path
    (backups, share materials, recovery).

    ``host_state`` is the workspace host's lifecycle state from the passive
    discovery resolver (see ``resolve_workspace_host_state``); a not-running
    state short-circuits everything without spawning anything, so a report
    filed from a stopped workspace resolves immediately instead of waiting out
    the exec budget.

    Never raises for a collection failure: every way this can go wrong resolves
    to the returned note.
    """
    if host_state is not None and host_state in _NOT_RUNNING_HOST_STATES:
        logger.info("Skipping bug-report diagnostics for {}: its host is {}", workspace_agent_id, host_state.value)
        return WorkspaceCollectionResult(
            note=f"the workspace host is {host_state.value.lower()}, so its logs and chats were not collected"
        )

    env = dict(os.environ)
    if latchkey_plugin_data_dir is not None:
        refresh_remote_gateway_log_mirror(
            workspace_agent_id,
            latchkey_plugin_data_dir=latchkey_plugin_data_dir,
            concurrency_group=concurrency_group,
            env=env,
            timeout_seconds=timeout_seconds,
        )

    argv = build_diagnostics_argv(workspace_agent_id, include_logs, include_transcript, timeout_seconds)
    try:
        stdout, _returncode, stderr = run_mngr_capturing(concurrency_group, argv, env, timeout_seconds=timeout_seconds)
    except MngrCommandTimeoutError:
        # Ordered before MngrCommandError, which it subclasses. A timeout
        # observed nothing, which is worth telling apart from a run that failed.
        logger.warning("Bug-report diagnostics for {} timed out", workspace_agent_id)
        return WorkspaceCollectionResult(note=f"workspace collection timed out after {int(timeout_seconds)}s")
    except MngrCommandError as exc:
        logger.warning("Bug-report diagnostics for {} could not run: {}", workspace_agent_id, exc)
        return WorkspaceCollectionResult(note=f"workspace collection could not run: {exc}")

    encoded = extract_exec_stdout(stdout)
    if encoded is None:
        # The envelope reported the remote command as failed (a workspace whose
        # template has no collector lands here) or was unreadable. In json mode
        # mngr's own stderr says nothing, so the diagnosis is read out of the
        # envelope itself -- the remote command's stderr, or the failed-agent
        # error of an exec that never landed -- with the process stderr tail as
        # the fallback for an envelope that could not even be parsed.
        logger.warning("Bug-report diagnostics for {} failed inside the workspace", workspace_agent_id)
        detail = (extract_exec_failure_detail(stdout) or stderr.strip())[-_NOTE_STDERR_MAX_CHARS:] or "no error output"
        return WorkspaceCollectionResult(note=f"workspace collection failed: {detail}")
    encoded = encoded.strip()
    if not encoded:
        return WorkspaceCollectionResult(note="the workspace collected nothing")
    try:
        # ``validate=True`` because the lenient default discards non-alphabet
        # characters silently, which would stage a truncated archive as if it
        # were whole. A value that will not decode raises ``ValueError``
        # (``binascii.Error`` is one) -- including the JSON line an older
        # template's collector prints.
        zip_bytes = base64.b64decode(encoded, validate=True)
        staged_path = staging_dir / STAGED_ZIP_FILENAME
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(zip_bytes)
    except ValueError:
        logger.warning("The workspace {} sent an unreadable payload", workspace_agent_id)
        return WorkspaceCollectionResult(
            note="the workspace sent an unreadable payload (its template may predate this app)"
        )
    except OSError as exc:
        logger.warning("Could not stage the bug-report workspace zip for {}: {}", workspace_agent_id, exc)
        return WorkspaceCollectionResult(note="the workspace archive could not be written to disk")
    logger.info("Bug-report diagnostics for {}: staged {}", workspace_agent_id, staged_path)
    return WorkspaceCollectionResult(staged_zip_path=staged_path)
