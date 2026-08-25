import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final

import pytest
from pydantic import TypeAdapter
from sentry_sdk.types import Event

from imbue.imbue_common.sentry.testing import capturing_sentry_client
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.report_collector import _report_title
from imbue.minds.desktop_client.report_collector import build_bug_report
from imbue.minds.desktop_client.report_collector import parse_attachment_flags
from imbue.minds.desktop_client.report_collector import submit_bug_report
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_diagnostics import TRANSCRIPT_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_LOGS_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceDiagnosticsOmissionReason

# What collection reports when the user asked for both attachments and neither could be produced: the
# real attachment keys and two different reasons from the closed set, so a test that swapped or
# dropped one would show it.
_ATTACHMENT_OMISSIONS: Final[Mapping[str, str]] = {
    WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.SECRETS_FOUND.value,
    TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CHAT_TRANSCRIPT.value,
}

_REPORT_ADAPTER: Final[TypeAdapter[dict[str, Any]]] = TypeAdapter(dict[str, Any])


def _build(
    description: str = "something broke",
    include_app_diagnostics: bool = False,
    remote_access_requested: bool = False,
    workspace_agent_id: str | None = None,
    session_store: MultiAccountSessionStore | None = None,
    backend_resolver: BackendResolverInterface | None = None,
    data_dir: Path | None = None,
) -> dict:
    return build_bug_report(
        description=description,
        include_app_diagnostics=include_app_diagnostics,
        remote_access_requested=remote_access_requested,
        workspace_agent_id=workspace_agent_id,
        session_store=session_store,
        backend_resolver=backend_resolver,
        data_dir=data_dir,
    )


def _build_with_attachments(
    *,
    include_logs: bool,
    include_transcript: bool,
    attachment_omissions: Mapping[str, str],
) -> dict:
    """Build a minimal report that carries the workspace-attachment fields.

    Kept separate from ``_build`` so that ``_build`` keeps exercising what a caller gets when it passes
    none of them.
    """
    return build_bug_report(
        description="something broke",
        include_app_diagnostics=False,
        remote_access_requested=False,
        workspace_agent_id=None,
        session_store=None,
        backend_resolver=None,
        data_dir=None,
        include_logs=include_logs,
        include_transcript=include_transcript,
        attachment_omissions=attachment_omissions,
    )


def _submitted_bug_report(event: Event) -> dict[str, Any]:
    """The structured report as it reached Sentry.

    ``Event`` types its ``extra`` as a mapping of ``object``, so the report is validated back into a
    mapping rather than assumed to be one.
    """
    extra: Mapping[str, Any] = event["extra"]
    return _REPORT_ADAPTER.validate_python(extra["bug_report"])


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
    report = _build(workspace_agent_id="agent-123")
    assert report["workspace"]["agent_id"] == "agent-123"


def test_build_bug_report_omits_workspace_when_not_in_a_workspace() -> None:
    # No workspace id -> the help flow was on a general screen, so there is no workspace section.
    assert "workspace" not in _build(workspace_agent_id=None)


def test_build_bug_report_reports_no_attachments_when_collection_never_ran() -> None:
    # A caller that does no workspace collection at all still produces the three attachment keys, so a
    # report is never silently missing them.
    report = _build()
    assert report["attachment_omissions"] == {}
    assert report["logs_requested"] is False
    assert report["transcript_requested"] is False


def test_build_bug_report_records_the_omission_reason_for_each_attachment() -> None:
    # The reason codes are the only record of why a requested attachment is absent -- without them a
    # report where collection failed is indistinguishable from one where the user asked for nothing.
    report = _build_with_attachments(
        include_logs=True,
        include_transcript=True,
        attachment_omissions=_ATTACHMENT_OMISSIONS,
    )
    assert report["attachment_omissions"] == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: "secrets_found",
        TRANSCRIPT_ATTACHMENT_KEY: "no_chat_transcript",
    }


def test_build_bug_report_records_which_attachments_the_user_asked_for() -> None:
    # Requested-but-absent and never-requested are different bugs to chase, so what the user ticked is
    # recorded next to what collection produced.
    report = _build_with_attachments(
        include_logs=True,
        include_transcript=False,
        attachment_omissions={TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED.value},
    )
    assert report["logs_requested"] is True
    assert report["transcript_requested"] is False


def test_build_bug_report_copies_attachment_omissions_into_a_serializable_mapping() -> None:
    # Collection can hand over a read-only mapping, and the report is serialized on its way to Sentry
    # -- json.dumps refuses a MappingProxyType outright.
    report = _build_with_attachments(
        include_logs=True,
        include_transcript=True,
        attachment_omissions=MappingProxyType(dict(_ATTACHMENT_OMISSIONS)),
    )
    assert json.loads(json.dumps(report))["attachment_omissions"] == dict(_ATTACHMENT_OMISSIONS)


def test_submit_bug_report_sends_the_attachment_omissions_to_sentry() -> None:
    # Everything above only matters if the omissions survive the submit: this is the one place the
    # reasons are readable, so pin them on the event that actually leaves the app.
    with capturing_sentry_client() as captured_events:
        event_id = submit_bug_report(
            description="boom",
            include_app_diagnostics=False,
            include_logs=True,
            include_transcript=True,
            remote_access_requested=False,
            workspace_agent_id=None,
            session_store=None,
            backend_resolver=None,
            data_dir=None,
            logs_folder=None,
            attachment_omissions=_ATTACHMENT_OMISSIONS,
        )

    assert event_id is not None
    assert len(captured_events) == 1
    report = _submitted_bug_report(captured_events[0])
    assert report["attachment_omissions"] == dict(_ATTACHMENT_OMISSIONS)
    assert report["logs_requested"] is True
    assert report["transcript_requested"] is True


def test_parse_attachment_flags_defaults_to_included_when_the_fields_are_absent() -> None:
    # The form's boxes are checked by default, so an omitted field means "attach it" -- reading absence
    # as False would silently drop the attachments for every /api/v1 caller.
    assert parse_attachment_flags({}) == (True, True)


def test_parse_attachment_flags_reads_the_two_fields_independently() -> None:
    # Guards against the two flags being crossed: unticking logs must not unattach the transcript.
    assert parse_attachment_flags({"include_logs": False, "include_transcript": True}) == (False, True)
    assert parse_attachment_flags({"include_logs": True, "include_transcript": False}) == (True, False)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        # A form-encoded or hand-written body sends strings; "false" is the one that must not read as
        # truthy, which is exactly what a bare bool() would do.
        ("true", True),
        ("false", False),
        ("False", False),
        (" false ", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
        # Anything else present is taken at face value rather than rejected: a bug report is never
        # failed over a malformed checkbox.
        ("maybe", True),
        (1, True),
        (0, False),
        ([], False),
        (["x"], True),
        # An explicit JSON null is indistinguishable from a field that was never sent, so it lands on
        # the same checked-by-default answer.
        (None, True),
    ],
)
def test_parse_attachment_flags_reads_a_flag_value(value: Any, expected: bool) -> None:
    assert parse_attachment_flags({"include_logs": value, "include_transcript": value}) == (expected, expected)
