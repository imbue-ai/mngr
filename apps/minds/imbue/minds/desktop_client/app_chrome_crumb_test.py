"""Unit tests for the titlebar breadcrumb name resolution on the chrome route.

The ``?agent=`` crumb name is resolved from the workspace rows the SSE stream
also feeds. Those rows are ``ChromeWorkspaceEntry`` models, so resolution reads
their attributes -- a dict-style lookup would raise ``AttributeError`` on a real
row. These tests drive the resolver with model rows directly.
"""

from imbue.minds.desktop_client.app import _resolve_crumb_workspace_name
from imbue.minds.desktop_client.chrome_state import ChromeWorkspaceEntry


def _entry(agent_id: str, name: str) -> ChromeWorkspaceEntry:
    return ChromeWorkspaceEntry(id=agent_id, name=name, accent="#123456")


def test_resolves_matching_workspace_name_from_model_rows() -> None:
    rows = [_entry("agent-aaa", "alpha"), _entry("agent-bbb", "beta")]
    # Reaching .name/.id on the model row is the whole point: a dict lookup here
    # raised "ChromeWorkspaceEntry object has no attribute get" on any real load.
    assert _resolve_crumb_workspace_name(rows, "agent-bbb") == "beta"


def test_no_crumb_id_yields_empty_string() -> None:
    rows = [_entry("agent-aaa", "alpha")]
    assert _resolve_crumb_workspace_name(rows, "") == ""


def test_unknown_crumb_id_yields_ellipsis_placeholder() -> None:
    rows = [_entry("agent-aaa", "alpha")]
    assert _resolve_crumb_workspace_name(rows, "agent-zzz") == "…"


def test_empty_rows_yield_ellipsis_placeholder() -> None:
    assert _resolve_crumb_workspace_name([], "agent-aaa") == "…"


def test_matching_row_without_a_name_falls_back_to_ellipsis() -> None:
    rows = [_entry("agent-aaa", "")]
    assert _resolve_crumb_workspace_name(rows, "agent-aaa") == "…"
