"""Gather a user-submitted bug report and hand it to Sentry.

Backs both the local "report a bug" form and the authenticated ``/api/v1`` bug-report route, so reports
from either path carry the same shape and are submitted the same way. All Sentry submission is owned by
the outer minds app -- agents never reach Sentry directly.

What is collected scales with what the user opted into: the description and a handful of always-cheap
"basics" (versions, OS) are unconditional; app diagnostics and per-workspace context are added only when
requested. Each collected value comes from an in-process source (build info, the session store, the
backend resolver, the standard library), so collection is fast and side-effect free.
"""

import os
import platform
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.utils.sentry.core import submit_manual_bug_report
from imbue.mngr.primitives import AgentId

_REPORT_TITLE_MAX_LENGTH: Final[int] = 120


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
    include_workspace_details: bool,
    remote_access_requested: bool,
    workspace_agent_id: str | None,
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface | None,
    data_dir: Path | None,
) -> dict[str, Any]:
    """Assemble the structured report attached to the Sentry event.

    ``remote_access_requested`` is recorded as a flag only -- no remote access is provisioned here.
    Workspace details are gathered only when both a ``workspace_agent_id`` is known and the user opted
    into them; otherwise the workspace section is omitted entirely (the help flow was not in a workspace).
    """
    report: dict[str, Any] = {
        "description": description,
        "basics": _collect_basics(),
        "remote_access_requested": remote_access_requested,
    }
    if include_app_diagnostics:
        report["app_diagnostics"] = _collect_app_diagnostics(
            session_store=session_store,
            backend_resolver=backend_resolver,
            data_dir=data_dir,
        )
    if workspace_agent_id and include_workspace_details:
        report["workspace"] = _collect_workspace_context(
            backend_resolver=backend_resolver,
            workspace_agent_id=workspace_agent_id,
        )
    return report


def submit_bug_report(
    *,
    description: str,
    include_logs: bool,
    include_app_diagnostics: bool,
    include_workspace_details: bool,
    remote_access_requested: bool,
    workspace_agent_id: str | None,
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface | None,
    data_dir: Path | None,
    logs_folder: Path | None,
) -> str | None:
    """Collect the report and submit it to Sentry.

    Returns the Sentry event id the user can quote when following up, or None when Sentry is inactive
    (e.g. dev/tests) or the event was dropped before sending.
    """
    report = build_bug_report(
        description=description,
        include_app_diagnostics=include_app_diagnostics,
        include_workspace_details=include_workspace_details,
        remote_access_requested=remote_access_requested,
        workspace_agent_id=workspace_agent_id,
        session_store=session_store,
        backend_resolver=backend_resolver,
        data_dir=data_dir,
    )
    return submit_manual_bug_report(
        title=_report_title(description),
        report=report,
        include_logs=include_logs,
        logs_folder=logs_folder,
    )


def submit_bug_report_from_body(
    *,
    body: Mapping[str, Any],
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface | None,
    minds_config: MindsConfig | None,
    paths: WorkspacePaths | None,
) -> str | None:
    """Parse a help-form / API request body and submit the resulting bug report.

    Shared by the local ``POST /help/report`` handler and the ``/api/v1`` bug-report route so both
    interpret the same fields identically. Logs are included when the persistent ``include_error_logs``
    setting is on OR the request opted in for this one report (the form surfaces that checkbox only when
    the setting is off). The caller is responsible for validating that a description is present.

    Returns the Sentry event id (or None when Sentry is inactive / the event was dropped).
    """
    include_logs_setting = minds_config.get_include_error_logs() if minds_config is not None else False
    include_logs = include_logs_setting or bool(body.get("include_logs", False))
    workspace_agent_id = body.get("workspace_agent_id") or None
    return submit_bug_report(
        description=str(body.get("description", "")).strip(),
        include_logs=include_logs,
        include_app_diagnostics=bool(body.get("include_app_diagnostics", False)),
        include_workspace_details=bool(body.get("include_workspace_details", False)),
        remote_access_requested=bool(body.get("remote_access", False)),
        workspace_agent_id=str(workspace_agent_id) if workspace_agent_id else None,
        session_store=session_store,
        backend_resolver=backend_resolver,
        data_dir=paths.data_dir if paths is not None else None,
        logs_folder=paths.log_dir if paths is not None else None,
    )
