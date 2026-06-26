from pathlib import Path

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.report_collector import _report_title
from imbue.minds.desktop_client.report_collector import build_bug_report
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore


def _build(
    description: str = "something broke",
    include_app_diagnostics: bool = False,
    include_workspace_details: bool = False,
    remote_access_requested: bool = False,
    workspace_agent_id: str | None = None,
    session_store: MultiAccountSessionStore | None = None,
    backend_resolver: BackendResolverInterface | None = None,
    data_dir: Path | None = None,
) -> dict:
    return build_bug_report(
        description=description,
        include_app_diagnostics=include_app_diagnostics,
        include_workspace_details=include_workspace_details,
        remote_access_requested=remote_access_requested,
        workspace_agent_id=workspace_agent_id,
        session_store=session_store,
        backend_resolver=backend_resolver,
        data_dir=data_dir,
    )


def test_report_title_uses_trimmed_first_line() -> None:
    assert _report_title("  first line  \nsecond line") == "[bug report] first line"


def test_report_title_falls_back_when_empty() -> None:
    assert _report_title("   \n  ") == "[bug report] (no description)"


def test_build_bug_report_always_includes_basics_and_description() -> None:
    report = _build(description="boom")
    assert report["description"] == "boom"
    assert "minds_release_id" in report["basics"]
    assert "platform" in report["basics"]


def test_build_bug_report_records_remote_access_flag_only() -> None:
    report = _build(remote_access_requested=True)
    assert report["remote_access_requested"] is True


def test_build_bug_report_omits_app_diagnostics_unless_requested() -> None:
    assert "app_diagnostics" not in _build(include_app_diagnostics=False)


def test_build_bug_report_includes_app_diagnostics_when_requested(tmp_path: Path) -> None:
    report = _build(include_app_diagnostics=True, data_dir=tmp_path)
    diagnostics = report["app_diagnostics"]
    assert "system" in diagnostics
    assert "cpu_count" in diagnostics["system"]
    assert "disk" in diagnostics["system"]


def test_build_bug_report_includes_workspace_context_when_in_a_workspace() -> None:
    # Even without a backend resolver, the workspace section carries at least the agent id.
    report = _build(include_workspace_details=True, workspace_agent_id="agent-123")
    assert report["workspace"]["agent_id"] == "agent-123"


def test_build_bug_report_omits_workspace_when_not_in_a_workspace() -> None:
    # No workspace id -> the help flow was on a general screen, so there is no workspace section.
    assert "workspace" not in _build(include_workspace_details=True, workspace_agent_id=None)


def test_build_bug_report_omits_workspace_when_details_not_requested() -> None:
    assert "workspace" not in _build(include_workspace_details=False, workspace_agent_id="agent-123")
