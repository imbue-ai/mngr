"""Collect the bug report's workspace attachments via the template's resident collector.

The workspace template ships a collector script inside every workspace
(``WORKSPACE_COLLECTOR_PATH``). It gathers the workspace logs and the recent
chat transcripts, secret-scans each one as plaintext in place with the
template's own scan gate, and prints ONE JSON line carrying everything that
survived as a single base64 zip. Minds' half is deliberately tiny: a ~300-byte
probe-and-run ``mngr exec`` that prints a READY sentinel and runs the collector
when the script exists, or a COLLECTOR-MISSING sentinel when the workspace was
built from a template that predates it. Nothing is delivered into the workspace
any more -- the previous design base64-injected the collector, a scanner, and
the console into one argv string, and Linux's 128KB single-argument cap made an
oversized console silently cost a report every attachment.

The Electron shell's console tail never travels to the workspace either. It is
staged app-side by this module, unscanned -- a deliberate decision, consistent
with ``electron.log`` and ``minds.log``, which already upload unscanned on
every event -- so its only omission reasons are ``not_requested`` and
``no_console_output``.

Everything here degrades rather than fails. A stopped host, an unreachable
container, a slow exec, or a workspace without the collector all resolve to an
omission plus a reason code from the closed
:class:`WorkspaceDiagnosticsOmissionReason` set, recorded in the report's
``attachment_omissions``. A bug report is never blocked by its own diagnostics,
so ``collect_workspace_diagnostics`` raises nothing for a collection failure.
The collector's own verdicts (``secrets_found``, ``scanner_unavailable``,
``no_chat_transcript``) arrive in the payload's ``omissions`` and pass through
to the report verbatim.

The staged files are attached one-shot, by exact path, to the report they were
collected for (``submit_manual_bug_report``'s ``report_file_paths``); no Sentry
attachment group matches them, so they can never ride along on an unrelated
error event's sweep. Every collection stages under its own slug
(``bug-report-<slug>-workspace.zip`` and ``bug-report-<slug>-console.log``), so
collections that overlap -- reports submit immediately and upload in the
background -- cannot clobber each other's files, and no report has to delete an
earlier one's staging to keep it from attaching. Disk is bounded instead by
``sweep_stale_staged_diagnostics_files``.
"""

import base64
import json
import os
import time
from collections.abc import Mapping
from enum import auto
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# Where the workspace template installs the resident collector. Part of the
# frozen collection contract with default-workspace-template's
# ``system/scripts/collect_bug_report_diagnostics.py``.
WORKSPACE_COLLECTOR_PATH: Final[str] = "/home/user/workspace/system/scripts/collect_bug_report_diagnostics.py"

# Printed by the outer shell before the collector runs. Its presence in stdout
# is the single bit that says the exec actually reached the container: without
# it every other field is absent because the plumbing failed, not because the
# collector found nothing.
DIAGNOSTICS_SENTINEL: Final[str] = "===DIAGNOSTICS-READY==="

# Printed instead of the READY sentinel when the workspace has no collector
# script: its template predates the resident-collector contract. Everything the
# exec was asked for reports ``collector_unavailable``.
COLLECTOR_MISSING_SENTINEL: Final[str] = "===DIAGNOSTICS-COLLECTOR-MISSING==="

# The one payload shape this host understands. The collector prints it back so
# a future incompatible payload announces itself instead of parsing as garbage.
DIAGNOSTICS_CONTRACT_VERSION: Final[int] = 1

# Whole-collection budget. Passed to ``mngr exec --timeout`` (capping the
# in-container work) and used as the outer subprocess timeout (capping the
# provider round-trip on top of it), so a wedged workspace cannot stretch a bug
# report's submission beyond this. The collector carries its own, shorter
# scanner timeout so a slow secret scan degrades to ``scanner_unavailable``
# instead of blowing the whole budget.
#
# Nobody waits on this: reports are filed immediately and collect on a
# background strand, and a workspace that is simply down is short-circuited by
# the host-stopped check rather than by this. So the budget only has to exceed a
# healthy collection, and the cost of it being too SMALL is much worse than it
# being too large -- exceeding it costs the report every workspace attachment.
#
# Sized from measurements inside a running workspace, where the collector asks
# the workspace's own mngr what its agents are and what was said in them:
#   mngr list                 65.8s
#   mngr event (per chat)     84.5s
#   secret scan (14 files)     9.2s
# so one chat agent is ~160s and each further chat adds ~85s. 300s covers three
# chats with room; at 60s even the single-chat case could not finish, which is
# what left every attachment reporting ``scanner_unavailable``.
#
# Those mngr timings are the thing to fix rather than live with -- they are
# suspected to be provider probing that cannot succeed inside a container
# (``mngr list --format json`` fails there outright), not mngr's own cost. If
# that is confirmed, this comes back down; reading the same state off the
# filesystem measured ~5s.
WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS: Final[float] = 300.0

# The slice of the collection budget the in-container secret scan may spend, so
# a slow scan degrades to ``scanner_unavailable`` instead of timing out the
# whole exec. A fraction rather than a constant: when a test runs collection
# under a longer budget (a shared CI sandbox is several times slower than the
# user machines the production budget is policy for), the scanner's share must
# stretch with it, or the scan starves while the exec idles.
_SCAN_TIMEOUT_FRACTION_OF_BUDGET: Final[float] = 0.6

# Keys naming each collectable content type. Shared with the collector payload's
# ``omissions`` and with the report's ``attachment_omissions`` mapping.
WORKSPACE_LOGS_ATTACHMENT_KEY: Final[str] = "workspace_logs"
TRANSCRIPT_ATTACHMENT_KEY: Final[str] = "transcript"
CONSOLE_ATTACHMENT_KEY: Final[str] = "console"

# Keys naming each staged FILE, which is no longer one per content type: the
# collector hands back a single zip whose members cover both the workspace logs
# and the transcript, so those two content keys share one staged file. The
# console still stages as its own file. ``WorkspaceDiagnosticsResult`` explains
# how the two key families relate.
WORKSPACE_ZIP_ATTACHMENT_KEY: Final[str] = "workspace_zip"

# Staged filenames under the minds logs dir, as
# ``bug-report-<collection slug>-<attachment><suffix>``. The slug is per
# collection, so two collections running at once stage side by side instead of
# overwriting each other; the shared prefix is what the stale sweep below
# recognizes. The workspace zip holds one member per collected file, so it
# stages as ``.zip``; the console is plain text and stays ``.log``. Neither
# suffix is matched by any minds attachment group's glob (``*.jsonl``,
# ``minds.log``, ...), so a staged file can never be swept onto an unrelated
# error event.
STAGED_FILENAME_PREFIX: Final[str] = "bug-report-"

STAGED_FILENAME_SUFFIX_BY_KEY: Final[Mapping[str, str]] = {
    WORKSPACE_ZIP_ATTACHMENT_KEY: ".zip",
    CONSOLE_ATTACHMENT_KEY: ".log",
}

_STAGED_FILENAME_STEM_BY_KEY: Final[Mapping[str, str]] = {
    WORKSPACE_ZIP_ATTACHMENT_KEY: "workspace",
    CONSOLE_ATTACHMENT_KEY: "console",
}

# Every suffix staging can produce, which is what the stale sweep has to look
# for: a sweep that knew only one of them would leave the other's files on disk
# forever.
_STAGED_FILENAME_SUFFIXES: Final[frozenset[str]] = frozenset(STAGED_FILENAME_SUFFIX_BY_KEY.values())

# How old a staged file must be before the sweep may remove it. Long enough that
# no in-flight background upload could still be reading one: the collection
# budget is 60s and the uploads that follow are ordinary S3 puts.
STALE_STAGED_FILE_MAX_AGE_SECONDS: Final[float] = 3600.0

# Host states in which the container is definitively not running, so an ``mngr
# exec --no-start`` could only wait out its timeout. All of them report the same
# ``host_stopped`` omission: the user-visible fact is that there was no running
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


class WorkspaceDiagnosticsOmissionReason(LowerCaseStrEnum):
    """Why a requested bug-report attachment is not present.

    A closed set: every failure mode of the collection path lands on exactly one
    of these, and the values are the wire format recorded in the Sentry event's
    ``attachment_omissions`` extra.
    """

    NOT_REQUESTED = auto()
    """The user left this attachment's checkbox unticked, so nothing was collected."""

    HOST_STOPPED = auto()
    """The passive discovery snapshot reads the workspace host as not running, so
    collection was skipped outright rather than waiting out the exec timeout."""

    EXEC_FAILED = auto()
    """The ``mngr exec`` never reached the in-container collector (no sentinel), or
    the collector returned neither content nor a reason for this attachment."""

    EXEC_TIMEOUT = auto()
    """The ``mngr exec`` was killed by the collection budget before it finished."""

    COLLECTOR_UNAVAILABLE = auto()
    """The workspace has no resident collector script: it was built from a template
    that predates the collection contract, so nothing in it can be collected."""

    SCANNER_UNAVAILABLE = auto()
    """The workspace's secret-scan gate could not run, so nothing was released: an
    unscanned file must never leave the workspace."""

    SECRETS_FOUND = auto()
    """The secret scanner reported findings for this content, so it was dropped."""

    NO_CHAT_TRANSCRIPT = auto()
    """The workspace has no chat transcript to send (no chat has ever been opened)."""

    NO_CONSOLE_OUTPUT = auto()
    """The shell has no captured console output to send (it never ran, or never printed)."""


class WorkspaceDiagnosticsResult(FrozenModel):
    """What collection produced: the staged files, and why anything else is missing.

    The two mappings use two related key families. ``staged_paths`` is keyed by
    staged FILE (``workspace_zip``, ``console``); ``attachment_omissions`` is
    keyed by CONTENT type (``workspace_logs``, ``transcript``, ``console``),
    because the workspace zip is one file covering two content types. A content
    key absent from the omissions is present in its staged file: the console in
    the staged console log, the workspace logs and transcript as members of the
    staged workspace zip.
    """

    staged_paths: Mapping[str, Path] = Field(
        default_factory=dict,
        description="Staged-file key -> the staged file written under the logs dir.",
    )
    attachment_omissions: Mapping[str, str] = Field(
        default_factory=dict,
        description="Content key -> the WorkspaceDiagnosticsOmissionReason value explaining its absence.",
    )


class DiagnosticsPayload(FrozenModel):
    """The JSON object the resident collector prints after the READY sentinel.

    An instance means the sentinel landed -- the exec reached the collector. An
    instance with no zip and no omissions is a real (if degenerate) observation
    and is deliberately distinct from ``None``, which means the exec never got
    inside at all.
    """

    contract_version: int | None = Field(
        default=None,
        description="The collector's payload version, or None when it sent none we could read.",
    )
    zip_base64: str | None = Field(
        default=None,
        description="Base64 of the zip holding everything that scanned clean; absent when nothing was collected.",
    )
    omissions: Mapping[str, str] = Field(
        default_factory=dict,
        description="Content key -> in-container reason code for the content that was withheld.",
    )


def build_diagnostics_shell_command(
    include_logs: bool,
    include_transcript: bool,
    timeout_seconds: float = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS,
) -> str:
    """Return the shell command minds passes to ``mngr exec``.

    A probe and a run, nothing else: if the workspace has the resident
    collector, print the READY sentinel and run it with the flags for the
    requested content; otherwise print the COLLECTOR-MISSING sentinel. No
    script, scanner, or console travels in -- the command stays a few hundred
    bytes, far from the 128KB single-argument cap that made the old inline
    payloads silently fail whole collections.
    """
    flags = []
    if include_logs:
        flags.append("--logs")
    if include_transcript:
        flags.append("--transcript")
    flags.append(f"--scan-timeout={int(timeout_seconds * _SCAN_TIMEOUT_FRACTION_OF_BUDGET)}")
    flag_text = " ".join(flags)
    return (
        f'p={WORKSPACE_COLLECTOR_PATH}; if [ -f "$p" ]; then '
        f"echo '{DIAGNOSTICS_SENTINEL}'; "
        f'python3 "$p" {flag_text}; '
        f"else echo '{COLLECTOR_MISSING_SENTINEL}'; fi"
    )


def build_diagnostics_argv(
    mngr_binary: str,
    workspace_agent_id: AgentId,
    include_logs: bool,
    include_transcript: bool,
    timeout_seconds: float = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS,
) -> list[str]:
    """Build the ``mngr exec`` argv that collects the diagnostics inside the workspace.

    ``--quiet`` suppresses mngr's own progress chatter so stdout starts with the
    sentinel directly. ``--no-start`` keeps a bug report from booting a stopped
    workspace just by asking it for logs. ``timeout_seconds`` defaults to the
    production budget; only tests pass anything else (a shared CI sandbox is
    several times slower than the user machines the budget is policy for).
    """
    return [
        mngr_binary,
        "exec",
        str(workspace_agent_id),
        build_diagnostics_shell_command(include_logs, include_transcript, timeout_seconds),
        "--timeout",
        str(int(timeout_seconds)),
        "--no-start",
        "--quiet",
    ]


def _coerce_str_mapping(value: object) -> dict[str, str]:
    """Coerce one of the payload's members into a ``str -> str`` mapping.

    Anything that is not an object of strings is dropped rather than trusted: the
    payload crosses a process boundary, so its shape is an assumption, not a
    guarantee.
    """
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if isinstance(item, str)}


def is_collector_missing(stdout: str | None) -> bool:
    """Whether the exec reached the workspace and found no resident collector there.

    The probe prints exactly one of the two sentinels, so this is checked before
    the READY parse: a missing collector is an answer (``collector_unavailable``),
    not a plumbing failure.
    """
    return stdout is not None and COLLECTOR_MISSING_SENTINEL in stdout


def parse_diagnostics_payload(stdout: str | None) -> DiagnosticsPayload | None:
    """Parse the collection exec's stdout into the collector's payload.

    Returns None when the READY sentinel never landed (stdout is None, or the
    exec returned without ever invoking the collector) -- the caller reports
    that as ``exec_failed`` (or ``collector_unavailable``, which it checks via
    ``is_collector_missing`` first). A sentinel followed by nothing usable still
    returns a payload: the exec demonstrably got inside, so the absence is the
    collector's observation rather than a plumbing failure.
    """
    if stdout is None or DIAGNOSTICS_SENTINEL not in stdout:
        return None
    after = stdout.split(DIAGNOSTICS_SENTINEL, 1)[1]
    json_line = next((line.strip() for line in after.splitlines() if line.strip()), None)
    if json_line is None:
        return DiagnosticsPayload()
    try:
        payload = json.loads(json_line)
    except json.JSONDecodeError as exc:
        logger.warning("The workspace collector emitted a non-JSON payload line ({!r}): {}", json_line[:200], exc)
        return DiagnosticsPayload()
    if not isinstance(payload, dict):
        return DiagnosticsPayload()
    raw_version = payload.get("contract_version")
    contract_version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else None
    if contract_version != DIAGNOSTICS_CONTRACT_VERSION:
        # Parsed best-effort anyway: the shape coercions below already drop what
        # they cannot read, and a partial answer beats none.
        logger.warning(
            "The workspace collector spoke contract version {!r}, not {}", raw_version, DIAGNOSTICS_CONTRACT_VERSION
        )
    raw_zip = payload.get("zip")
    return DiagnosticsPayload(
        contract_version=contract_version,
        # Still base64 here: staging is what decodes it, so a garbled value
        # degrades to an omission there instead of a parse failure here.
        zip_base64=raw_zip if isinstance(raw_zip, str) else None,
        omissions=_coerce_str_mapping(payload.get("omissions")),
    )


def build_staged_diagnostics_filename(staged_file_key: str, collection_slug: str) -> str:
    """Return the staged filename one collection writes a file under."""
    stem = _STAGED_FILENAME_STEM_BY_KEY[staged_file_key]
    return f"{STAGED_FILENAME_PREFIX}{collection_slug}-{stem}{STAGED_FILENAME_SUFFIX_BY_KEY[staged_file_key]}"


def sweep_stale_staged_diagnostics_files(logs_dir: Path) -> None:
    """Delete staged bug-report files in ``logs_dir`` older than the stale age.

    Not a "clear the previous report's files" step: staged names are unique per
    collection, so there is nothing to clobber and nothing an old file can ride
    along on (attachments are one-shot, by exact path). Deleting a previous
    report's files at submit time would instead race the background upload still
    reading them. Sweeping only what is old enough that no upload could still
    want it bounds disk without that race. A file that cannot be removed is
    logged rather than raised -- it costs disk, not the report.

    Candidates are every staged suffix, not one of them: files stage under two
    (the console's ``.log`` and the workspace archive's ``.zip``), and a sweep
    that recognized only one would let the other accumulate on disk forever.
    """
    cutoff = time.time() - STALE_STAGED_FILE_MAX_AGE_SECONDS
    try:
        candidates = sorted(
            path for path in logs_dir.glob(f"{STAGED_FILENAME_PREFIX}*") if path.suffix in _STAGED_FILENAME_SUFFIXES
        )
    except OSError as exc:
        logger.warning("Could not sweep stale staged bug-report files in {}: {}", logs_dir, exc)
        return
    for path in candidates:
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove the stale staged bug-report file {}: {}", path, exc)


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


def _coerce_omission_reason(raw: str | None) -> WorkspaceDiagnosticsOmissionReason:
    """Map an in-container reason string onto the closed set.

    A missing or unrecognized reason means the collector returned neither content
    nor an explanation we understand, which is a collection failure like any
    other.
    """
    if raw is None:
        return WorkspaceDiagnosticsOmissionReason.EXEC_FAILED
    try:
        return WorkspaceDiagnosticsOmissionReason(raw)
    except ValueError:
        logger.warning("The workspace collector returned an unrecognized omission reason: {!r}", raw)
        return WorkspaceDiagnosticsOmissionReason.EXEC_FAILED


def _run_collection_exec(
    concurrency_group: ConcurrencyGroup, argv: list[str], env: dict[str, str], timeout_seconds: float
) -> str:
    """Run the collection ``mngr exec`` and return its stdout.

    A nonzero exit is deliberately not an error: the sentinel in stdout is the
    evidence that matters, and a collector that printed its payload before
    something downstream exited badly is still worth parsing. Raises
    ``MngrCommandTimeoutError`` when the budget elapsed and ``MngrCommandError``
    when the process could not be launched at all.
    """
    try:
        finished = concurrency_group.run_process_to_completion(
            argv,
            timeout=timeout_seconds,
            is_checked_after=False,
            env=env,
        )
    except (OSError, ConcurrencyGroupError) as exc:
        raise MngrCommandError(str(exc)) from exc
    if finished.is_timed_out:
        raise MngrCommandTimeoutError(f"timed out after {int(timeout_seconds)}s")
    return finished.stdout


def _stage_file(logs_dir: Path, staged_file_key: str, collection_slug: str, data: bytes) -> Path:
    """Write one staged file under this collection's slug, returning its path.

    Raises ``OSError`` like any write; the callers degrade that to an omission.
    """
    path = logs_dir / build_staged_diagnostics_filename(staged_file_key, collection_slug)
    logs_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _requested_workspace_keys(include_logs: bool, include_transcript: bool) -> tuple[str, ...]:
    """The content keys the exec is asked to collect from inside the workspace.

    The console is not among them: it is the shell's own output, staged app-side
    without ever travelling to the workspace.
    """
    keys: list[str] = []
    if include_logs:
        keys.append(WORKSPACE_LOGS_ATTACHMENT_KEY)
    if include_transcript:
        keys.append(TRANSCRIPT_ATTACHMENT_KEY)
    return tuple(keys)


def _workspace_failure_result(
    workspace_keys: tuple[str, ...],
    staged_paths: dict[str, Path],
    omissions: dict[str, str],
    reason: WorkspaceDiagnosticsOmissionReason,
) -> WorkspaceDiagnosticsResult:
    """Result for a collection whose exec produced nothing, for one shared reason.

    Only the workspace content keys share it: the console never rides the exec,
    so whatever it already staged (or reported) stands.
    """
    return WorkspaceDiagnosticsResult(
        staged_paths=staged_paths,
        attachment_omissions={**omissions, **{key: reason for key in workspace_keys}},
    )


def _stage_console(
    console_text: str | None,
    logs_dir: Path,
    collection_slug: str,
    staged_paths: dict[str, Path],
    omissions: dict[str, str],
) -> None:
    """Stage the shell's console tail directly, recording the omission when there is none.

    No travel and no scan, by decision: the console attaches unscanned, exactly
    like ``electron.log`` and ``minds.log`` already do on every event.
    """
    if console_text is None:
        omissions[CONSOLE_ATTACHMENT_KEY] = WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT
        return
    try:
        staged_paths[CONSOLE_ATTACHMENT_KEY] = _stage_file(
            logs_dir, CONSOLE_ATTACHMENT_KEY, collection_slug, console_text.encode("utf-8")
        )
    except OSError as exc:
        logger.warning("Could not stage the bug-report console for this collection: {}", exc)
        # The console's reasons are a closed pair (it never execs, so the exec
        # reasons would be lies); an unwritable logs dir reads as no console.
        omissions[CONSOLE_ATTACHMENT_KEY] = WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT


def collect_workspace_diagnostics(
    workspace_agent_id: AgentId,
    *,
    include_logs: bool,
    include_transcript: bool,
    logs_dir: Path,
    host_state: HostState | None,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    timeout_seconds: float = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS,
    console_text: str | None = None,
) -> WorkspaceDiagnosticsResult:
    """Stage the requested bug-report attachments.

    For the workspace content the user asked for, runs one probe-and-run ``mngr
    exec`` against the template's resident collector and stages the zip it hands
    back under ``logs_dir``, using filenames carrying this collection's own slug
    so a concurrent collection cannot overwrite them. Old staged files are swept
    on the way in (see ``sweep_stale_staged_diagnostics_files``); recent ones
    are left alone, because a report still uploading in the background is
    reading them.

    ``console_text`` is the shell's captured console tail; it rides on the logs
    checkbox and is staged directly by this function -- it never travels to the
    workspace and is deliberately not scanned. None means the shell has no
    captured output, which reports as ``no_console_output``. Because the console
    does not depend on the exec, it still stages when the host is stopped, the
    exec fails, or the workspace has no collector.

    ``host_state`` is the workspace host's lifecycle state from the passive
    discovery resolver (see ``resolve_workspace_host_state``); a not-running
    state short-circuits to ``host_stopped`` without spawning anything, so a
    report filed from a stopped workspace submits immediately instead of waiting
    out the exec budget.

    Never raises for a collection failure. Every way this can go wrong resolves
    to an omission reason on the returned result.
    """
    sweep_stale_staged_diagnostics_files(logs_dir)
    collection_slug = uuid4().hex
    staged_paths: dict[str, Path] = {}
    omissions: dict[str, str] = {}
    if not include_transcript:
        omissions[TRANSCRIPT_ATTACHMENT_KEY] = WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED
    if include_logs:
        # The console rides on the logs checkbox: it is the shell's own output,
        # but a user who declined logs declined the console with them.
        _stage_console(console_text, logs_dir, collection_slug, staged_paths, omissions)
    else:
        omissions[WORKSPACE_LOGS_ATTACHMENT_KEY] = WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED
        omissions[CONSOLE_ATTACHMENT_KEY] = WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED

    workspace_keys = _requested_workspace_keys(include_logs, include_transcript)
    if not workspace_keys:
        return WorkspaceDiagnosticsResult(staged_paths=staged_paths, attachment_omissions=omissions)

    if host_state is not None and host_state in _NOT_RUNNING_HOST_STATES:
        logger.info("Skipping bug-report diagnostics for {}: its host is {}", workspace_agent_id, host_state.value)
        return _workspace_failure_result(
            workspace_keys, staged_paths, omissions, WorkspaceDiagnosticsOmissionReason.HOST_STOPPED
        )

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    argv = build_diagnostics_argv(mngr_binary, workspace_agent_id, include_logs, include_transcript, timeout_seconds)
    try:
        stdout = _run_collection_exec(concurrency_group, argv, env, timeout_seconds)
    except MngrCommandTimeoutError as exc:
        # Ordered before MngrCommandError, which it subclasses. A timeout observed
        # nothing, which is worth telling apart from an exec that ran and failed.
        logger.warning("Bug-report diagnostics for {} timed out: {}", workspace_agent_id, exc)
        return _workspace_failure_result(
            workspace_keys, staged_paths, omissions, WorkspaceDiagnosticsOmissionReason.EXEC_TIMEOUT
        )
    except MngrCommandError as exc:
        logger.warning("Bug-report diagnostics for {} could not run: {}", workspace_agent_id, exc)
        return _workspace_failure_result(
            workspace_keys, staged_paths, omissions, WorkspaceDiagnosticsOmissionReason.EXEC_FAILED
        )

    if is_collector_missing(stdout):
        logger.info(
            "The workspace {} has no resident diagnostics collector; its template predates it", workspace_agent_id
        )
        return _workspace_failure_result(
            workspace_keys, staged_paths, omissions, WorkspaceDiagnosticsOmissionReason.COLLECTOR_UNAVAILABLE
        )
    payload = parse_diagnostics_payload(stdout)
    if payload is None:
        logger.warning("Bug-report diagnostics for {} never reached the container", workspace_agent_id)
        return _workspace_failure_result(
            workspace_keys, staged_paths, omissions, WorkspaceDiagnosticsOmissionReason.EXEC_FAILED
        )

    for key in workspace_keys:
        raw_reason = payload.omissions.get(key)
        if raw_reason is not None:
            omissions[key] = _coerce_omission_reason(raw_reason)
    # The keys whose content the zip is expected to carry: requested, and not
    # explained away by the collector's own omissions.
    covered_keys = tuple(key for key in workspace_keys if key not in omissions)
    if payload.zip_base64 is None:
        # A requested content type with neither a zip to live in nor a reason is
        # a collection failure like any other.
        return _workspace_failure_result(
            covered_keys, staged_paths, omissions, WorkspaceDiagnosticsOmissionReason.EXEC_FAILED
        )
    try:
        # ``validate=True`` because the lenient default discards non-alphabet
        # characters silently, which would stage a truncated archive as if it
        # were whole. A value that will not decode raises ``ValueError``
        # (``binascii.Error`` is one) and costs the zip, never the report.
        zip_bytes = base64.b64decode(payload.zip_base64, validate=True)
        staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY] = _stage_file(
            logs_dir, WORKSPACE_ZIP_ATTACHMENT_KEY, collection_slug, zip_bytes
        )
    except (OSError, ValueError) as exc:
        logger.warning("Could not stage the bug-report workspace zip for {}: {}", workspace_agent_id, exc)
        return _workspace_failure_result(
            covered_keys, staged_paths, omissions, WorkspaceDiagnosticsOmissionReason.EXEC_FAILED
        )
    logger.info(
        "Bug-report diagnostics for {}: staged {}, omitted {}",
        workspace_agent_id,
        sorted(staged_paths),
        dict(sorted(omissions.items())),
    )
    return WorkspaceDiagnosticsResult(staged_paths=staged_paths, attachment_omissions=omissions)
