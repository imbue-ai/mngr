"""Gather a user-submitted bug report and hand it to Sentry.

Backs both the local "report a bug" form and the authenticated ``/api/v1`` bug-report route, so reports
from either path carry the same shape and are submitted the same way. All Sentry submission is owned by
the outer minds app -- agents never reach Sentry directly.

What is collected depends on the report's context: the description and a handful of always-cheap
"basics" (versions, OS) are unconditional; app diagnostics are added when requested, and per-workspace
context whenever the report was opened from a known workspace. Each collected value comes from an in-process source (build info, the session store, the
backend resolver, the standard library), so collection is fast and side-effect free. The workspace
logs and chat transcript a report can carry are the exception: they are collected out of process by
:mod:`workspace_diagnostics` into one staged archive, and reach this module either as that staged
file plus at most one plain-words note about it, or -- when the report was accepted before its
collection finished -- as the S3 location the archive has been reserved at, with the note pointing
at the status document that will record how the collection went.
"""

import os
import platform
import shutil
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.sentry.core import ErrorAttachmentsS3Uploader
from imbue.imbue_common.sentry.core import get_attachments_uploader
from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.console_log_staging import read_console_tail
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceCollectionResult
from imbue.minds.desktop_client.workspace_diagnostics import collect_workspace_diagnostics
from imbue.minds.desktop_client.workspace_diagnostics import make_staging_dir
from imbue.minds.desktop_client.workspace_diagnostics import resolve_workspace_host_state
from imbue.minds.utils.sentry.core import submit_manual_bug_report
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.store import plugin_data_dir

_REPORT_TITLE_MAX_LENGTH: Final[int] = 120

# Request-body fields carrying the form's two attachment checkboxes.
_INCLUDE_LOGS_FIELD: Final[str] = "include_logs"
_INCLUDE_TRANSCRIPT_FIELD: Final[str] = "include_transcript"

# Spellings a non-boolean flag value can use to mean "unchecked". Anything else present is truthy.
_FALSE_FLAG_STRINGS: Final[frozenset[str]] = frozenset({"", "0", "false", "no", "off"})

# A report built without any workspace-attachment collection has nothing to explain.
_NO_REPORT_FILE_PATHS: Final[Mapping[str, Path]] = MappingProxyType({})
_NO_REPORT_FILE_URIS: Final[Mapping[str, str | None]] = MappingProxyType({})


def _parse_include_flag(body: Mapping[str, Any], key: str) -> bool:
    """Read one attachment checkbox out of a request body, defaulting to True when it is absent.

    The form's boxes are checked by default, so an omitted field means "included" -- and an
    ``/api/v1`` caller that never sends the field lands on that same default. A string value is read
    the way a form encodes one, so ``"false"`` is not mistaken for truth.
    """
    value = body.get(key)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_FLAG_STRINGS
    return bool(value)


def parse_attachment_flags(body: Mapping[str, Any]) -> tuple[bool, bool]:
    """Read the two attachment checkboxes out of a request body, as ``(include_logs, include_transcript)``.

    The one place a request's boxes are interpreted, so the collection a report
    runs and the flags recorded on its event can never read the same request
    differently.
    """
    return (
        _parse_include_flag(body, _INCLUDE_LOGS_FIELD),
        _parse_include_flag(body, _INCLUDE_TRANSCRIPT_FIELD),
    )


def _report_title(description: str) -> str:
    """Derive a concise Sentry event title from the user's description (its trimmed first line)."""
    stripped = description.strip()
    first_line = stripped.splitlines()[0] if stripped else ""
    title = first_line[:_REPORT_TITLE_MAX_LENGTH].strip()
    return f"[bug report] {title}" if title else "[bug report] (no description)"


def _collect_basics() -> dict[str, Any]:
    """Always-included, always-cheap identifying facts about this install."""
    return {
        "minds_release_id": resolve_release_id(),
        "minds_git_sha": resolve_git_sha(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }


def _collect_system_usage(data_dir: Path | None) -> dict[str, Any]:
    """Lightweight host resource snapshot using only the standard library (no extra dependency)."""
    usage: dict[str, Any] = {"cpu_count": os.cpu_count()}
    # getloadavg is available on macOS/Linux (the only platforms minds targets), but guard anyway.
    if hasattr(os, "getloadavg"):
        load_1m, load_5m, load_15m = os.getloadavg()
        usage["load_average"] = {"1m": load_1m, "5m": load_5m, "15m": load_15m}
    total, used, free = shutil.disk_usage(data_dir if data_dir is not None else Path.home())
    usage["disk"] = {"total_bytes": total, "used_bytes": used, "free_bytes": free}
    return usage


def _collect_app_diagnostics(
    *,
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface | None,
    data_dir: Path | None,
) -> dict[str, Any]:
    """Minds-app state available everywhere: signed-in accounts, known workspaces, host resource use."""
    diagnostics: dict[str, Any] = {"system": _collect_system_usage(data_dir)}
    if session_store is not None:
        diagnostics["signed_in_account_emails"] = [account.email for account in session_store.list_accounts()]
    if backend_resolver is not None:
        diagnostics["known_workspace_ids"] = [
            str(agent_id) for agent_id in backend_resolver.list_known_workspace_ids()
        ]
        diagnostics["active_workspace_ids"] = [
            str(agent_id) for agent_id in backend_resolver.list_active_workspace_ids()
        ]
        diagnostics["initial_discovery_complete"] = backend_resolver.has_completed_initial_discovery()
    return diagnostics


def _collect_workspace_context(
    *,
    backend_resolver: BackendResolverInterface | None,
    workspace_agent_id: str,
) -> dict[str, Any]:
    """Context for the workspace the help flow was opened from (only meaningful when in a workspace)."""
    context: dict[str, Any] = {"agent_id": workspace_agent_id}
    if backend_resolver is not None:
        info = backend_resolver.get_agent_display_info(AgentId(workspace_agent_id))
        if info is not None:
            context["agent_name"] = info.agent_name
            context["host_id"] = info.host_id
            context["provider_name"] = info.provider_name
    return context


def build_bug_report(
    *,
    description: str,
    include_app_diagnostics: bool,
    remote_access_requested: bool,
    workspace_agent_id: str | None,
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface | None,
    data_dir: Path | None,
    include_logs: bool = False,
    include_transcript: bool = False,
    collection_note: str | None = None,
) -> dict[str, Any]:
    """Assemble the structured report attached to the Sentry event.

    ``remote_access_requested`` is recorded as a flag only -- no remote access is provisioned here.
    Workspace details are gathered whenever a ``workspace_agent_id`` is known; otherwise the workspace
    section is omitted entirely (the help flow was not opened from a workspace).

    The two attachment flags record what the user asked to attach.
    ``collection_note`` is one plain-words sentence about the workspace
    attachment when there is something to say -- why there is no archive, or
    that collection is still running and where its outcome will land. None
    (recorded as null) means the archive speaks for itself.
    """
    report: dict[str, Any] = {
        "description": description,
        "basics": _collect_basics(),
        "remote_access_requested": remote_access_requested,
        "logs_requested": include_logs,
        "transcript_requested": include_transcript,
        "collection_note": collection_note,
    }
    if include_app_diagnostics:
        report["app_diagnostics"] = _collect_app_diagnostics(
            session_store=session_store,
            backend_resolver=backend_resolver,
            data_dir=data_dir,
        )
    if workspace_agent_id:
        report["workspace"] = _collect_workspace_context(
            backend_resolver=backend_resolver,
            workspace_agent_id=workspace_agent_id,
        )
    return report


def submit_bug_report(
    *,
    description: str,
    include_app_diagnostics: bool,
    include_logs: bool,
    include_transcript: bool,
    remote_access_requested: bool,
    workspace_agent_id: str | None,
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface | None,
    data_dir: Path | None,
    logs_folder: Path | None,
    collection_note: str | None = None,
    report_file_paths: Mapping[str, Path] = _NO_REPORT_FILE_PATHS,
    report_file_uris: Mapping[str, str | None] = _NO_REPORT_FILE_URIS,
) -> str | None:
    """Collect the report and submit it to Sentry.

    The workspace archive and console tail are staged by the caller before this
    runs, so what lands here is what the user asked for (the two include
    flags), the staged files themselves (``report_file_paths``, attached
    one-shot to exactly this event -- never swept by the process-global groups,
    which would carry them onto unrelated error events), and at most one
    ``collection_note`` sentence about the archive when it needs one.

    A report whose collection is still running instead passes
    ``report_file_uris``: S3 locations reserved for files that do not exist
    yet, published on the event now and written to by the background collection
    later. Reserving is purely local, so this is what lets a report be accepted
    without the user waiting on the workspace.

    Returns the Sentry event id the user can quote when following up, or None when Sentry is inactive
    (e.g. dev/tests) or the event was dropped before sending.
    """
    report = build_bug_report(
        description=description,
        include_app_diagnostics=include_app_diagnostics,
        remote_access_requested=remote_access_requested,
        workspace_agent_id=workspace_agent_id,
        session_store=session_store,
        backend_resolver=backend_resolver,
        data_dir=data_dir,
        include_logs=include_logs,
        include_transcript=include_transcript,
        collection_note=collection_note,
    )
    return submit_manual_bug_report(
        title=_report_title(description),
        description=description,
        report=report,
        logs_folder=logs_folder,
        report_file_paths=report_file_paths,
        report_file_uris=report_file_uris,
    )


# Extras names for the one-shot report files: the workspace archive and the
# shell's captured console tail. The names are the ``uploaded_files_<name>``
# suffixes existing Sentry queries key on.
_WORKSPACE_REPORT_FILE_NAME: Final[str] = "bug_report_workspace"
_CONSOLE_REPORT_FILE_NAME: Final[str] = "bug_report_console"

# Extras name for the short document a background-path report finishes with,
# recording what the workspace archive ended up doing. Following it is how a
# report whose collection was still running at capture time becomes readable.
_REPORT_ATTACHMENT_STATUS_FILE_KEY: Final[str] = "bug_report_attachment_status"

# The console tail's filename inside the report's staging dir. Plain text, so
# unlike the archive it is gzipped on the way up.
_CONSOLE_STAGED_FILENAME: Final[str] = "console.log"

# What the pending path records on the event: the archive's outcome cannot be
# known at capture time, and this says where it will be.
_PENDING_COLLECTION_NOTE: Final[str] = (
    "the workspace archive is being collected in the background; its outcome is in bug_report_attachment_status"
)


def _staged_console(logs_dir: Path | None, staging_dir: Path) -> Path | None:
    """Stage the shell's captured console tail app-side, or None when there is none.

    The console is the shell's own output, staged directly and deliberately
    unscanned (the same standing as ``electron.log`` and ``minds.log``), so no
    workspace, exec, or scanner is involved -- which is why every submit path
    can resolve it at plan time, before the event is captured, workspace or
    not. A tail that cannot be read or written costs the console file only,
    with the why in minds' own log (which rides every report).
    """
    if logs_dir is None:
        return None
    console_text = read_console_tail(logs_dir)
    if console_text is None:
        return None
    path = staging_dir / _CONSOLE_STAGED_FILENAME
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(console_text.encode("utf-8"))
    except OSError as exc:
        logger.warning("Could not stage the bug-report console tail: {}", exc)
        return None
    return path


class _PendingReportCollection(FrozenModel):
    """The workspace collection a submitted report still owes, dispatched after capture."""

    state: DesktopClientState = Field(description="Desktop state the collection reads discovery from.")
    workspace_agent_id: str = Field(description="The workspace the report was filed from.")
    include_logs: bool = Field(description="Whether the workspace logs were requested.")
    include_transcript: bool = Field(description="Whether the recent chats were requested.")
    staging_dir: Path = Field(description="The report's own staging directory collection writes into.")
    concurrency_group: ConcurrencyGroup = Field(description="Group the collection strand runs in.")
    uploader: ErrorAttachmentsS3Uploader = Field(description="Uploader whose reserved keys the bytes go to.")
    reserved_zip_key: str = Field(description="Reserved S3 key the staged archive uploads to.")
    status_key: str = Field(description="Reserved S3 key for the final status document.")

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}


class _ReportAttachments(FrozenModel):
    """What a submit can say about its attachments at the moment its event is captured.

    Either the collection is already resolved -- ``report_file_paths`` names
    real files and ``collection_note`` is final -- or it is still owed, in
    which case ``report_file_uris`` publishes where the files will land, the
    note says the outcome is in the status document, and ``pending_collection``
    is the work to dispatch once the event is away. The console is never
    pending: it is staged app-side at plan time either way.
    """

    collection_note: str | None = Field(
        default=None, description="One plain-words sentence about the workspace archive, when there is one to say."
    )
    report_file_paths: Mapping[str, Path] = Field(
        default_factory=dict, description="Extras name -> an already-staged file to attach one-shot."
    )
    report_file_uris: Mapping[str, str | None] = Field(
        default_factory=dict, description="Extras name -> the reserved uri its bytes will be readable at."
    )
    pending_collection: _PendingReportCollection | None = Field(
        default=None, description="The collection to run in the background, or None when nothing is owed."
    )

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}


def _resolved_report_attachments(
    note: str | None,
    *,
    include_logs: bool,
    logs_dir: Path | None,
    staging_dir: Path,
) -> _ReportAttachments:
    """Attachments for a report whose workspace collection will never run.

    ``note`` says why, when there is anything to say (a report filed outside a
    workspace has nothing: the request flags already tell that story). The
    console is independent of the workspace: with logs ticked, the captured
    tail still stages and attaches.
    """
    console_path = _staged_console(logs_dir, staging_dir) if include_logs else None
    return _ReportAttachments(
        collection_note=note,
        report_file_paths={_CONSOLE_REPORT_FILE_NAME: console_path} if console_path is not None else {},
    )


def _plan_report_attachments(
    state: DesktopClientState,
    *,
    workspace_agent_id: str,
    include_logs: bool,
    include_transcript: bool,
) -> _ReportAttachments:
    """Decide what this report can say about its attachments without making the user wait.

    Collection reaches into the workspace and takes seconds to minutes, so it
    never runs on the request thread: the archive's S3 key is *reserved* --
    minting one is purely local -- and the collection is left to a background
    strand, letting the user have their report id now. The console needs no
    collection, so it is staged and resolved here on every path.

    Either way the files are attached one-shot, by exact path or exact reserved
    key, to this report alone -- deliberately not via the process-global
    attachment groups, which sweep on every event and would carry one report's
    consented files onto every unrelated automatic error.
    """
    # Each report stages into its own fresh temp dir: concurrent reports cannot
    # collide, and nothing on disk is ever deleted up front.
    staging_dir = make_staging_dir()
    paths = state.api_v1_paths
    logs_dir = paths.log_dir if paths is not None else None
    concurrency_group = state.root_concurrency_group
    if not workspace_agent_id:
        # The help flow was opened outside a workspace: the workspace
        # checkboxes were never shown and there is no container to collect
        # from. The request flags tell that story; no note needed.
        return _resolved_report_attachments(
            None, include_logs=include_logs, logs_dir=logs_dir, staging_dir=staging_dir
        )
    if not (include_logs or include_transcript):
        # Both boxes unticked: nothing to collect, nothing to reserve, and the
        # recorded flags say so.
        return _resolved_report_attachments(
            None, include_logs=include_logs, logs_dir=logs_dir, staging_dir=staging_dir
        )
    if concurrency_group is None:
        # No root strand to run the collection exec on, and none to run it in
        # the background either (minimal apps only, e.g. tests).
        return _resolved_report_attachments(
            "workspace collection could not run in this app",
            include_logs=include_logs,
            logs_dir=logs_dir,
            staging_dir=staging_dir,
        )

    uploader = get_attachments_uploader()
    if uploader is None:
        # Sentry was never set up (dev and tests), so no attachment has anywhere
        # to go and there is nothing to reserve. Collect inline: the wait this
        # costs cannot happen in a shipped app, where setup_sentry always
        # registers an uploader.
        result = _collect_for_report(
            state,
            workspace_agent_id=workspace_agent_id,
            include_logs=include_logs,
            include_transcript=include_transcript,
            staging_dir=staging_dir,
        )
        console_path = _staged_console(logs_dir, staging_dir) if include_logs else None
        report_file_paths: dict[str, Path] = {}
        if console_path is not None:
            report_file_paths[_CONSOLE_REPORT_FILE_NAME] = console_path
        if result.staged_zip_path is not None:
            report_file_paths[_WORKSPACE_REPORT_FILE_NAME] = result.staged_zip_path
        return _ReportAttachments(collection_note=result.note, report_file_paths=report_file_paths)

    console_path = _staged_console(logs_dir, staging_dir) if include_logs else None
    # Reserved with the ``.zip`` suffix the archive stages under, so the key
    # names the zip it will hold (uploaded as-is) instead of claiming a gzip a
    # reader would have to unwrap twice.
    reservations = uploader.reserve_report_file_uploads({_WORKSPACE_REPORT_FILE_NAME: ".zip"})
    zip_uri, zip_key = reservations[_WORKSPACE_REPORT_FILE_NAME]
    status_uri, status_key = uploader.reserve_text_upload(_REPORT_ATTACHMENT_STATUS_FILE_KEY)
    return _ReportAttachments(
        collection_note=_PENDING_COLLECTION_NOTE,
        report_file_paths={_CONSOLE_REPORT_FILE_NAME: console_path} if console_path is not None else {},
        report_file_uris={
            _WORKSPACE_REPORT_FILE_NAME: zip_uri,
            _REPORT_ATTACHMENT_STATUS_FILE_KEY: status_uri,
        },
        pending_collection=_PendingReportCollection(
            state=state,
            workspace_agent_id=workspace_agent_id,
            include_logs=include_logs,
            include_transcript=include_transcript,
            staging_dir=staging_dir,
            concurrency_group=concurrency_group,
            uploader=uploader,
            reserved_zip_key=zip_key,
            status_key=status_key,
        ),
    )


def _start_pending_report_collection(attachments: _ReportAttachments, *, event_id: str | None) -> None:
    """Dispatch the collection a report still owes onto its own strand.

    Called after the event is captured, so the report is already accepted: the
    strand only fills in the S3 objects the event already points at. It is
    unchecked so a failure cannot poison the root group, and a group that
    refuses the strand outright is logged and dropped -- an accepted report is
    never undone by its attachments.
    """
    pending = attachments.pending_collection
    if pending is None:
        return
    try:
        pending.concurrency_group.start_new_thread(
            target=collect_and_upload_report_attachments,
            kwargs={"pending": pending, "event_id": event_id},
            name=f"bug-report-attachments-{pending.workspace_agent_id}",
            daemon=True,
            is_checked=False,
        )
    except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
        logger.warning("Could not start the background bug-report collection for event {}: {}", event_id, exc)


def collect_and_upload_report_attachments(*, pending: _PendingReportCollection, event_id: str | None) -> None:
    """Collect an already-accepted report's archive and upload it to its reserved key.

    Runs off the request thread, so nothing here can fail the report -- that
    was submitted before this started. A collection that produced no archive
    leaves the reserved object absent; the status document uploaded last is
    what says which happened, either way.

    The strand is unchecked, and anything that escapes here is logged with its
    traceback by ``ObservableThread`` rather than surfacing anywhere the user
    (or the root group) can see it.
    """
    logger.info("Collecting the bug-report workspace archive for event {} in the background", event_id)
    result = _collect_for_report(
        pending.state,
        workspace_agent_id=pending.workspace_agent_id,
        include_logs=pending.include_logs,
        include_transcript=pending.include_transcript,
        staging_dir=pending.staging_dir,
    )
    if result.staged_zip_path is not None:
        pending.uploader.upload_reserved_report_file(pending.reserved_zip_key, result.staged_zip_path)
    pending.uploader.upload_reserved_text(
        pending.status_key,
        _report_attachment_status_document(result, workspace_agent_id=pending.workspace_agent_id, event_id=event_id),
    )
    logger.info(
        "Background bug-report collection for event {} finished: {}",
        event_id,
        result.staged_zip_path or result.note,
    )


def _report_attachment_status_document(
    result: WorkspaceCollectionResult, *, workspace_agent_id: str, event_id: str | None
) -> str:
    """Render the background collection's outcome as readable text.

    The event that referenced these uploads was captured before the outcome was
    known, so this document is where a reader learns whether the archive
    arrived -- and, when it did not, the one-line why.
    """
    if result.staged_zip_path is not None:
        outcome = "attached"
    else:
        outcome = f"not attached ({result.note})"
    return f"bug report event: {event_id}\nworkspace: {workspace_agent_id}\n\nworkspace archive: {outcome}\n"


def _collect_for_report(
    state: DesktopClientState,
    *,
    workspace_agent_id: str,
    include_logs: bool,
    include_transcript: bool,
    staging_dir: Path,
) -> WorkspaceCollectionResult:
    """Run one collection for the boxes that are actually ticked.

    Nothing travels into the container -- the exec only names the flags for the
    resident collector -- and the ``mngr`` subprocesses inherit this process's
    environment, exactly like every other desktop-client exec path.
    """
    agent_id = AgentId(workspace_agent_id)
    supervisor = state.latchkey_forward_supervisor
    concurrency_group = state.root_concurrency_group
    assert concurrency_group is not None, "collection is only planned when a root concurrency group exists"
    return collect_workspace_diagnostics(
        agent_id,
        include_logs=include_logs,
        include_transcript=include_transcript,
        staging_dir=staging_dir,
        host_state=resolve_workspace_host_state(state.backend_resolver, agent_id),
        concurrency_group=concurrency_group,
        latchkey_plugin_data_dir=plugin_data_dir(supervisor.latchkey_directory) if supervisor is not None else None,
    )


def submit_report_with_attachments(*, body: Mapping[str, Any], state: DesktopClientState) -> str | None:
    """Parse a help-form / API request body, submit the report, and collect its attachments behind it.

    The whole flow in one place, shared by the local ``POST /help/report``
    handler and the ``/api/v1`` bug-report route so both interpret the same
    fields identically: decide what the report can say about its attachments,
    submit it, then dispatch whatever collection it still owes onto its own
    strand. The report is filed before any collection runs, so the user has
    their id in about a second. Remote access remains opt-in (Imbue does not
    look into a workspace without consent); the caller is responsible for
    validating that a description is present.

    The archive's S3 key is reserved BEFORE the event is captured, because a
    Sentry event is immutable once sent -- there is no attaching a pointer to
    it afterwards. Reserving is purely local (a timestamp plus a uuid4, no
    network), so the event can publish where the archive will be readable while
    the collection producing it has not started.

    Returns the Sentry event id (or None when Sentry is inactive / the event was
    dropped).
    """
    workspace_agent_id = str(body.get("workspace_agent_id") or "").strip()
    include_logs, include_transcript = parse_attachment_flags(body)
    attachments = _plan_report_attachments(
        state,
        workspace_agent_id=workspace_agent_id,
        include_logs=include_logs,
        include_transcript=include_transcript,
    )
    paths = state.api_v1_paths
    event_id = submit_bug_report(
        description=str(body.get("description", "")).strip(),
        include_app_diagnostics=True,
        include_logs=include_logs,
        include_transcript=include_transcript,
        remote_access_requested=bool(body.get("remote_access", False)),
        workspace_agent_id=workspace_agent_id or None,
        session_store=state.session_store,
        backend_resolver=state.backend_resolver,
        data_dir=paths.data_dir if paths is not None else None,
        logs_folder=paths.log_dir if paths is not None else None,
        collection_note=attachments.collection_note,
        report_file_paths=attachments.report_file_paths,
        report_file_uris=attachments.report_file_uris,
    )
    _start_pending_report_collection(attachments, event_id=event_id)
    return event_id
