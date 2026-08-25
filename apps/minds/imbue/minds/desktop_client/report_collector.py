"""Gather a user-submitted bug report and hand it to Sentry.

Backs both the local "report a bug" form and the authenticated ``/api/v1`` bug-report route, so reports
from either path carry the same shape and are submitted the same way. All Sentry submission is owned by
the outer minds app -- agents never reach Sentry directly.

What is collected depends on the report's context: the description and a handful of always-cheap
"basics" (versions, OS) are unconditional; app diagnostics are added when requested, and per-workspace
context whenever the report was opened from a known workspace. Each collected value comes from an in-process source (build info, the session store, the
backend resolver, the standard library), so collection is fast and side-effect free. The workspace
logs and chat transcript a report can carry are the exception: they are collected out of process by
:mod:`workspace_diagnostics`, and reach this module either as already-staged files plus the reasons
anything requested is missing, or -- when the report was accepted before its collection finished --
as the S3 locations those files have been reserved at, plus the keys still pending.
"""

import os
import platform
import shutil
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.sentry.core import ErrorAttachmentsS3Uploader
from imbue.imbue_common.sentry.core import get_attachments_uploader
from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.console_log_staging import read_console_tail
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.workspace_diagnostics import CONSOLE_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import STAGED_FILENAME_SUFFIX_BY_KEY
from imbue.minds.desktop_client.workspace_diagnostics import TRANSCRIPT_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_LOGS_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_ZIP_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceDiagnosticsOmissionReason
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceDiagnosticsResult
from imbue.minds.desktop_client.workspace_diagnostics import build_staged_diagnostics_filename
from imbue.minds.desktop_client.workspace_diagnostics import collect_workspace_diagnostics
from imbue.minds.desktop_client.workspace_diagnostics import resolve_workspace_host_state
from imbue.minds.desktop_client.workspace_diagnostics import sweep_stale_staged_diagnostics_files
from imbue.minds.utils.sentry.core import submit_manual_bug_report
from imbue.mngr.primitives import AgentId

_REPORT_TITLE_MAX_LENGTH: Final[int] = 120

# Request-body fields carrying the form's two attachment checkboxes.
_INCLUDE_LOGS_FIELD: Final[str] = "include_logs"
_INCLUDE_TRANSCRIPT_FIELD: Final[str] = "include_transcript"

# Spellings a non-boolean flag value can use to mean "unchecked". Anything else present is truthy.
_FALSE_FLAG_STRINGS: Final[frozenset[str]] = frozenset({"", "0", "false", "no", "off"})

# A report built without any workspace-attachment collection has nothing to explain.
_NO_ATTACHMENT_OMISSIONS: Final[Mapping[str, str]] = MappingProxyType({})
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

    Shared by the route -- which needs them before the report exists, to drive collection -- and by
    ``submit_bug_report_from_body``, so both read one request the same way.
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
    attachment_omissions: Mapping[str, str] = _NO_ATTACHMENT_OMISSIONS,
    attachments_pending: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble the structured report attached to the Sentry event.

    ``remote_access_requested`` is recorded as a flag only -- no remote access is provisioned here.
    Workspace details are gathered whenever a ``workspace_agent_id`` is known; otherwise the workspace
    section is omitted entirely (the help flow was not opened from a workspace).

    The two attachment flags record what the user asked to attach, and ``attachment_omissions`` maps
    each attachment key to why it is not there (see ``WorkspaceDiagnosticsOmissionReason``). Their
    defaults describe a report built without any workspace collection at all.

    ``attachments_pending`` names the attachments whose collection had not finished when the event was
    captured, so an absent key is not mistaken for a silent drop: those keys are deliberately not in
    ``attachment_omissions``, which is a closed set of *final* outcomes. What each of them ended up
    doing is recorded in the status document the report's uploads finish with.
    """
    report: dict[str, Any] = {
        "description": description,
        "basics": _collect_basics(),
        "remote_access_requested": remote_access_requested,
        "logs_requested": include_logs,
        "transcript_requested": include_transcript,
        "attachment_omissions": dict(attachment_omissions),
        "attachments_pending": list(attachments_pending),
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
    attachment_omissions: Mapping[str, str],
    report_file_paths: Mapping[str, Path] = _NO_REPORT_FILE_PATHS,
    report_file_uris: Mapping[str, str | None] = _NO_REPORT_FILE_URIS,
    attachments_pending: Sequence[str] = (),
) -> str | None:
    """Collect the report and submit it to Sentry.

    The workspace logs and chat transcript are staged by the caller before this runs, so what lands
    here is what the user asked for (the two include flags), what collection could not produce
    (``attachment_omissions``), and the staged files themselves (``report_file_paths``), attached
    one-shot to exactly this event -- never swept by the process-global groups, which would carry
    them onto unrelated error events.

    A report whose collection is still running instead passes ``report_file_uris``: S3 locations
    reserved for attachments that do not exist yet, published on the event now and written to by the
    background collection later, plus ``attachments_pending`` naming which keys those are. Reserving
    is purely local, so this is what lets a report be accepted without the user waiting on the
    workspace.

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
        attachment_omissions=attachment_omissions,
        attachments_pending=attachments_pending,
    )
    return submit_manual_bug_report(
        title=_report_title(description),
        description=description,
        report=report,
        logs_folder=logs_folder,
        report_file_paths=report_file_paths,
        report_file_uris=report_file_uris,
    )


def submit_bug_report_from_body(
    *,
    body: Mapping[str, Any],
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface | None,
    paths: InstallationPaths | None,
    attachment_omissions: Mapping[str, str],
    report_file_paths: Mapping[str, Path] = _NO_REPORT_FILE_PATHS,
    report_file_uris: Mapping[str, str | None] = _NO_REPORT_FILE_URIS,
    attachments_pending: Sequence[str] = (),
) -> str | None:
    """Parse a help-form / API request body and submit the resulting bug report.

    Shared by the local ``POST /help/report`` handler and the ``/api/v1`` bug-report route so both
    interpret the same fields identically. Recent logs and app diagnostics (app version, signed-in
    accounts, the list of workspaces, and host/system info -- no workspace contents) are always
    included, as are details of the workspace the report was opened from (its id, name, host, and
    provider -- no workspace contents). The workspace's own logs and chat transcript are opt-out
    (``include_logs`` / ``include_transcript``, absent meaning included, matching the form's
    checkboxes); the caller collects them beforehand and passes what it could not produce as
    ``attachment_omissions``. A caller whose collection is still running passes ``report_file_uris``
    and ``attachments_pending`` instead of staged paths (see ``submit_bug_report``). Remote access
    remains opt-in (Imbue does not look into a workspace without consent). The caller is responsible
    for validating that a description is present.

    Returns the Sentry event id (or None when Sentry is inactive / the event was dropped).
    """
    workspace_agent_id = body.get("workspace_agent_id") or None
    include_logs, include_transcript = parse_attachment_flags(body)
    return submit_bug_report(
        description=str(body.get("description", "")).strip(),
        include_app_diagnostics=True,
        include_logs=include_logs,
        include_transcript=include_transcript,
        remote_access_requested=bool(body.get("remote_access", False)),
        workspace_agent_id=str(workspace_agent_id) if workspace_agent_id else None,
        session_store=session_store,
        backend_resolver=backend_resolver,
        data_dir=paths.data_dir if paths is not None else None,
        logs_folder=paths.log_dir if paths is not None else None,
        attachment_omissions=attachment_omissions,
        report_file_paths=report_file_paths,
        report_file_uris=report_file_uris,
        attachments_pending=attachments_pending,
    )


# Extras names for the one-shot report files, keyed by staged FILE rather than
# by content type: the resident collector hands the workspace logs and the
# chat transcripts back as members of one archive, so they upload as one file.
# The console keeps the extras name its retired per-content upload used so
# existing Sentry queries keep working.
_REPORT_FILE_KEY_BY_STAGED_FILE_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        WORKSPACE_ZIP_ATTACHMENT_KEY: "bug_report_workspace",
        CONSOLE_ATTACHMENT_KEY: "bug_report_console",
    }
)

# Which staged file each content type lands in when it survives collection. The
# omission bookkeeping speaks content keys (that is what the report's
# ``attachment_omissions`` and the collector's manifest use), while files stage,
# reserve, and upload per staged-file key; this is the bridge between the two.
_STAGED_FILE_KEY_BY_CONTENT_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WORKSPACE_ZIP_ATTACHMENT_KEY,
        TRANSCRIPT_ATTACHMENT_KEY: WORKSPACE_ZIP_ATTACHMENT_KEY,
        CONSOLE_ATTACHMENT_KEY: CONSOLE_ATTACHMENT_KEY,
    }
)

# Extras name for the short document a background-path report finishes with,
# recording what each attachment finally did. It shares the ``uploaded_files_``
# shape of the attachments themselves, so a reader finds it beside them on the
# event -- and following it is how a report whose attachments were still pending
# at capture time becomes readable at all.
_REPORT_ATTACHMENT_STATUS_FILE_KEY: Final[str] = "bug_report_attachment_status"


def _stage_console_tail(logs_dir: Path) -> Path | None:
    """Stage the shell's captured console tail app-side, or None when there is none to stage.

    The submit paths that never run a collection still owe the console when logs
    were ticked: it is the shell's own output, staged directly and deliberately
    unscanned (the same standing as ``electron.log`` and ``minds.log``), so no
    workspace, exec, or scanner is needed to attach it. The collection path
    stages it inside ``collect_workspace_diagnostics`` instead; this covers the
    reports that never get that far. A tail that cannot be read or written costs
    the console file only, reported as ``no_console_output``.
    """
    console_text = read_console_tail(logs_dir)
    if console_text is None:
        return None
    path = logs_dir / build_staged_diagnostics_filename(CONSOLE_ATTACHMENT_KEY, uuid4().hex)
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(console_text.encode("utf-8"))
    except OSError as exc:
        logger.warning("Could not stage the bug-report console tail: {}", exc)
        return None
    return path


class _ReportAttachments(FrozenModel):
    """What a submit can say about its attachments at the moment its event is captured.

    Either the collection is already done -- ``report_file_paths`` names real
    files, ``attachment_omissions`` is final for every key -- or it is still
    owed, in which case ``report_file_uris`` publishes where the files will land,
    ``pending_attachment_keys`` says which keys those are, and
    ``pending_collection`` is the work to dispatch once the event is away.
    """

    attachment_omissions: Mapping[str, str] = Field(
        description="Content key -> its final omission reason; pending keys are deliberately absent."
    )
    report_file_paths: Mapping[str, Path] = Field(
        default_factory=dict, description="Extras name -> an already-staged file to attach one-shot."
    )
    report_file_uris: Mapping[str, str | None] = Field(
        default_factory=dict, description="Extras name -> the reserved uri its bytes will be readable at."
    )
    pending_attachment_keys: tuple[str, ...] = Field(
        default=(), description="Content keys whose outcome is not known yet."
    )
    pending_collection: Mapping[str, Any] | None = Field(
        default=None, description="The collection to run in the background, or None when nothing is owed."
    )

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}


def _resolved_report_attachments(
    reason: WorkspaceDiagnosticsOmissionReason,
    *,
    include_logs: bool,
    include_transcript: bool,
    logs_dir: Path | None,
) -> _ReportAttachments:
    """Attachments for a report whose workspace collection will never run, for one shared reason.

    ``reason`` explains only the workspace content the user asked for; an
    unticked checkbox always reads as ``not_requested``, matching what
    ``collect_workspace_diagnostics`` reports on the normal path. The console is
    different: it is the shell's own output and needs neither the workspace nor
    the collection exec, so with logs ticked the captured tail still stages and
    attaches -- workspace or not. ``logs_dir`` of None means there is nowhere to
    even read the tail from, which reports as ``no_console_output``.
    """
    omissions: dict[str, str] = {
        WORKSPACE_LOGS_ATTACHMENT_KEY: (
            reason if include_logs else WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED
        ).value,
        TRANSCRIPT_ATTACHMENT_KEY: (
            reason if include_transcript else WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED
        ).value,
    }
    report_file_paths: dict[str, Path] = {}
    if not include_logs:
        # The console rides on the logs checkbox: a user who declined logs
        # declined the console with them.
        omissions[CONSOLE_ATTACHMENT_KEY] = WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED.value
    else:
        staged_console = _stage_console_tail(logs_dir) if logs_dir is not None else None
        if staged_console is None:
            omissions[CONSOLE_ATTACHMENT_KEY] = WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT.value
        else:
            report_file_paths[_REPORT_FILE_KEY_BY_STAGED_FILE_KEY[CONSOLE_ATTACHMENT_KEY]] = staged_console
    return _ReportAttachments(attachment_omissions=omissions, report_file_paths=report_file_paths)


def _plan_report_attachments(
    state: DesktopClientState,
    *,
    workspace_agent_id: str,
    include_logs: bool,
    include_transcript: bool,
) -> _ReportAttachments:
    """Decide what this report can say about its attachments without making the user wait.

    Collection reaches into the workspace and takes seconds, so it never runs on
    the request thread: the attachments are *reserved* -- an S3 key is minted
    locally, so the event can name where each file will be readable -- and the
    collection is left to a background strand, letting the user have their
    report id now.

    Either way the files are attached one-shot, by exact path or exact reserved
    key, to this report alone -- deliberately not via the process-global
    attachment groups, which sweep on every event and would carry one report's
    consented files onto every unrelated automatic error.
    """
    paths = state.api_v1_paths
    if paths is None:
        # No logs dir: nothing can be staged (not even the console, whose
        # captured tail lives there), so no attachment could be produced.
        return _resolved_report_attachments(
            WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
            include_logs=include_logs,
            include_transcript=include_transcript,
            logs_dir=None,
        )
    logs_dir = paths.log_dir
    # Every submit bounds the staged-file disk, whether or not it goes on to
    # collect; only files old enough that no background upload could still be
    # reading one are removed.
    sweep_stale_staged_diagnostics_files(logs_dir)
    concurrency_group = state.root_concurrency_group
    if not workspace_agent_id:
        # The help flow was opened outside a workspace, so the workspace
        # checkboxes were never shown and there is no container to collect
        # from -- but the console is the shell's own output and attaches
        # anyway when logs are on (the default the form's absence implies).
        # It stages unscanned by decision, exactly as it does from a
        # workspace, so being outside one no longer costs the report its
        # console.
        return _resolved_report_attachments(
            WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED,
            include_logs=include_logs,
            include_transcript=include_transcript,
            logs_dir=logs_dir,
        )
    if concurrency_group is None:
        # No root strand to run the collection exec on, and none to run it in the
        # background either (minimal apps only, e.g. tests): the report is
        # submitted immediately, with nothing pending, saying so. The console
        # needs no exec, so it still stages.
        return _resolved_report_attachments(
            WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
            include_logs=include_logs,
            include_transcript=include_transcript,
            logs_dir=logs_dir,
        )
    if not (include_logs or include_transcript):
        # Both boxes unticked: there is nothing to collect (the console rides
        # the logs box), so nothing to reserve, dispatch, or wait for.
        return _resolved_report_attachments(
            WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED,
            include_logs=include_logs,
            include_transcript=include_transcript,
            logs_dir=logs_dir,
        )

    uploader = get_attachments_uploader()
    if uploader is None:
        # Sentry was never set up (dev and tests), so no attachment has anywhere
        # to go and there is nothing to reserve. Collect inline: it is the one
        # arrangement that still produces real omission reasons, and the wait it
        # costs cannot happen in a shipped app, where setup_sentry always
        # registers an uploader.
        return _collected_report_attachments(
            state,
            workspace_agent_id=workspace_agent_id,
            include_logs=include_logs,
            include_transcript=include_transcript,
            logs_dir=logs_dir,
            concurrency_group=concurrency_group,
        )

    pending_keys = _requested_attachment_keys(include_logs=include_logs, include_transcript=include_transcript)
    staged_file_keys = _expected_staged_file_keys(include_logs=include_logs, include_transcript=include_transcript)
    # Reserved per staged FILE, with the suffix each will be staged under, so the workspace archive's
    # key names the zip it will hold (uploaded as-is) instead of claiming a gzip a reader would have
    # to unwrap twice.
    reservations = uploader.reserve_report_file_uploads(
        {_REPORT_FILE_KEY_BY_STAGED_FILE_KEY[key]: STAGED_FILENAME_SUFFIX_BY_KEY[key] for key in staged_file_keys}
    )
    status_uri, status_key = uploader.reserve_text_upload(_REPORT_ATTACHMENT_STATUS_FILE_KEY)
    return _ReportAttachments(
        # Only the boxes the user left unticked have a final answer yet; the rest
        # are pending, and inventing a reason for them would put a made-up final
        # outcome in the one place their real one is recorded.
        attachment_omissions={
            key: WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED.value
            for key in _STAGED_FILE_KEY_BY_CONTENT_KEY
            if key not in pending_keys
        },
        report_file_uris={
            **{name: uri for name, (uri, _key) in reservations.items()},
            _REPORT_ATTACHMENT_STATUS_FILE_KEY: status_uri,
        },
        pending_attachment_keys=pending_keys,
        # Plain kwargs for the strand rather than a model: it needs no state of
        # its own, only what to collect and where the bytes go.
        pending_collection={
            "state": state,
            "workspace_agent_id": workspace_agent_id,
            "include_logs": include_logs,
            "include_transcript": include_transcript,
            "logs_dir": logs_dir,
            "concurrency_group": concurrency_group,
            "uploader": uploader,
            "reserved_key_by_staged_file_key": {
                key: reservations[_REPORT_FILE_KEY_BY_STAGED_FILE_KEY[key]][1] for key in staged_file_keys
            },
            "pending_attachment_keys": pending_keys,
            "status_key": status_key,
        },
    )


def _requested_attachment_keys(*, include_logs: bool, include_transcript: bool) -> tuple[str, ...]:
    """The content keys the ticked boxes ask for, in a stable order.

    The console rides on the logs checkbox: it is the shell's own output, so
    ticking logs asks for it too, staged app-side beside whatever the workspace
    returns.
    """
    requested_by_key = {
        WORKSPACE_LOGS_ATTACHMENT_KEY: include_logs,
        CONSOLE_ATTACHMENT_KEY: include_logs,
        TRANSCRIPT_ATTACHMENT_KEY: include_transcript,
    }
    return tuple(key for key, is_requested in requested_by_key.items() if is_requested)


def _expected_staged_file_keys(*, include_logs: bool, include_transcript: bool) -> tuple[str, ...]:
    """The staged files a collection for these boxes could produce, in a stable order.

    Not one per content type: the workspace logs and the chat transcripts come
    back as members of one archive, so either box alone expects the one zip; the
    console (riding the logs box) stages as its own file.
    """
    keys: list[str] = []
    if include_logs or include_transcript:
        keys.append(WORKSPACE_ZIP_ATTACHMENT_KEY)
    if include_logs:
        keys.append(CONSOLE_ATTACHMENT_KEY)
    return tuple(keys)


def _collected_report_attachments(
    state: DesktopClientState,
    *,
    workspace_agent_id: str,
    include_logs: bool,
    include_transcript: bool,
    logs_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> _ReportAttachments:
    """Collect on the calling thread and return attachments with nothing left pending."""
    result = _collect_for_report(
        state,
        workspace_agent_id=workspace_agent_id,
        include_logs=include_logs,
        include_transcript=include_transcript,
        logs_dir=logs_dir,
        concurrency_group=concurrency_group,
    )
    return _ReportAttachments(
        attachment_omissions=result.attachment_omissions,
        report_file_paths={
            _REPORT_FILE_KEY_BY_STAGED_FILE_KEY[key]: path for key, path in result.staged_paths.items()
        },
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
    concurrency_group = pending["concurrency_group"]
    try:
        concurrency_group.start_new_thread(
            target=collect_and_upload_report_attachments,
            kwargs={**pending, "event_id": event_id},
            name=f"bug-report-attachments-{pending['workspace_agent_id']}",
            daemon=True,
            is_checked=False,
        )
    except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
        logger.warning("Could not start the background bug-report collection for event {}: {}", event_id, exc)


def collect_and_upload_report_attachments(
    *,
    state: DesktopClientState,
    workspace_agent_id: str,
    include_logs: bool,
    include_transcript: bool,
    logs_dir: Path,
    concurrency_group: ConcurrencyGroup,
    uploader: ErrorAttachmentsS3Uploader,
    reserved_key_by_staged_file_key: Mapping[str, str],
    pending_attachment_keys: Sequence[str],
    status_key: str,
    event_id: str | None,
) -> None:
    """Collect an already-accepted report's attachments and upload them to their reserved keys.

    Deliberately dumb: it is told what to collect and which reserved key each
    staged file goes to, and it does exactly that. It runs off the request
    thread, so nothing here can fail the report -- that was submitted before
    this started. Each surviving staged file goes to the exact key whose uri the
    event published; anything that did not survive leaves its reserved object
    absent, and the status document uploaded last is what says which happened to
    each attachment.

    The strand is unchecked, and anything that escapes here is logged with its
    traceback by ``ObservableThread`` rather than surfacing anywhere the user
    (or the root group) can see it.
    """
    logger.info(
        "Collecting bug-report attachments for event {} in the background: {}",
        event_id,
        sorted(reserved_key_by_staged_file_key),
    )
    result = _collect_for_report(
        state,
        workspace_agent_id=workspace_agent_id,
        include_logs=include_logs,
        include_transcript=include_transcript,
        logs_dir=logs_dir,
        concurrency_group=concurrency_group,
    )
    for staged_file_key, reserved_key in reserved_key_by_staged_file_key.items():
        staged_path = result.staged_paths.get(staged_file_key)
        if staged_path is None:
            continue
        uploader.upload_reserved_report_file(reserved_key, staged_path)
    uploader.upload_reserved_text(
        status_key,
        _report_attachment_status_document(
            pending_attachment_keys,
            result,
            workspace_agent_id=workspace_agent_id,
            event_id=event_id,
        ),
    )
    logger.info(
        "Background bug-report attachments for event {} finished: staged {}, omitted {}",
        event_id,
        sorted(result.staged_paths),
        dict(sorted(result.attachment_omissions.items())),
    )


def _report_attachment_status_document(
    pending_attachment_keys: Sequence[str],
    result: WorkspaceDiagnosticsResult,
    *,
    workspace_agent_id: str,
    event_id: str | None,
) -> str:
    """Render the per-attachment outcome of a background collection as readable text.

    The event that referenced these uploads was captured before any of this was
    known, so this document is the only place a reader can learn what actually
    happened to each attachment -- ``attached`` for content that reached its
    staged file, or the omission reason that closed it out. It speaks CONTENT
    keys (the same vocabulary as ``attachment_omissions`` and the pending list
    on the event), while the uploads themselves go per staged file.
    """
    lines = [
        f"bug report event: {event_id}",
        f"workspace: {workspace_agent_id}",
        "",
    ]
    for content_key in sorted(pending_attachment_keys):
        # The omission is checked first: a zip can stage for one content type
        # while the other was withheld, so "its staged file exists" alone does
        # not mean this content is in it.
        reason = result.attachment_omissions.get(content_key)
        if reason is not None:
            outcome = reason
        elif _STAGED_FILE_KEY_BY_CONTENT_KEY[content_key] in result.staged_paths:
            outcome = "attached"
        else:
            outcome = WorkspaceDiagnosticsOmissionReason.EXEC_FAILED.value
        lines.append(f"{content_key}: {outcome}")
    return "\n".join(lines) + "\n"


def _collect_for_report(
    state: DesktopClientState,
    *,
    workspace_agent_id: str,
    include_logs: bool,
    include_transcript: bool,
    logs_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> WorkspaceDiagnosticsResult:
    """Run one collection for the boxes that are actually ticked.

    The console tail is only read when logs were asked for, so an unticked box
    contributes nothing. Nothing travels into the container either way -- the
    exec only names the flags for the resident collector.
    """
    agent_id = AgentId(workspace_agent_id)
    return collect_workspace_diagnostics(
        agent_id,
        include_logs=include_logs,
        include_transcript=include_transcript,
        logs_dir=logs_dir,
        host_state=resolve_workspace_host_state(state.backend_resolver, agent_id),
        mngr_binary=state.mngr_binary,
        mngr_host_dir=state.mngr_host_dir,
        concurrency_group=concurrency_group,
        console_text=read_console_tail(logs_dir) if include_logs else None,
    )


def submit_report_with_attachments(*, body: Mapping[str, Any], state: DesktopClientState) -> str | None:
    """Submit a bug report and collect its workspace attachments behind it.

    The whole flow in one place: decide what the report can say about its
    attachments, submit it, then dispatch whatever collection it still owes onto
    its own strand. The report is filed before any collection runs, so the user
    has their id in about a second.

    The S3 keys for a pending collection are reserved BEFORE the event is
    captured, because a Sentry event is immutable once sent -- there is no
    attaching a pointer to it afterwards. Reserving is purely local (a timestamp
    plus a uuid4, no network), so the event can publish where each attachment
    will be readable while the collection producing it has not started.
    """
    workspace_agent_id = str(body.get("workspace_agent_id") or "").strip()
    include_logs, include_transcript = parse_attachment_flags(body)
    attachments = _plan_report_attachments(
        state,
        workspace_agent_id=workspace_agent_id,
        include_logs=include_logs,
        include_transcript=include_transcript,
    )
    event_id = submit_bug_report_from_body(
        body=body,
        session_store=state.session_store,
        backend_resolver=state.backend_resolver,
        paths=state.api_v1_paths,
        attachment_omissions=attachments.attachment_omissions,
        report_file_paths=attachments.report_file_paths,
        report_file_uris=attachments.report_file_uris,
        attachments_pending=attachments.pending_attachment_keys,
    )
    _start_pending_report_collection(attachments, event_id=event_id)
    return event_id
