"""Unit tests for :class:`FileSharingGrantHandler`.

These exercise the handler's pure methods (request-type claim, display
name, and detail-fragment rendering) in process. The HTTP-dispatcher
tests that stand up the full desktop-client app live in
``test_latchkey_handlers.py``.
"""

import json
from html.parser import HTMLParser
from pathlib import Path

import httpx

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.handlers.testing import make_file_sharing_handler
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_file_sharing_permission_request_event
from imbue.mngr.primitives import AgentId


def _parse_input_attrs(html: str, element_id: str) -> dict[str, str]:
    """Return the attribute map of the ``<input>`` with the given id.

    Used to assert that user-controlled values are safely encoded into
    attributes (the HTML parser sees the real attribute boundaries, so a
    successful breakout would surface as extra attributes rather than a
    value substring).
    """
    found: dict[str, str] = {}

    class _Finder(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag != "input":
                return
            attr_map = {name: (value or "") for name, value in attrs}
            if attr_map.get("id") == element_id:
                found.update(attr_map)

    _Finder().feed(html)
    if not found:
        raise AssertionError(f"no <input> with id={element_id!r} found in HTML")
    return found


# -- handler.handles_request_type --


def test_handler_claims_file_sharing_request_type(tmp_path: Path) -> None:
    handler, _sender = make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    assert handler.handles_request_type() == str(RequestType.FILE_SHARING_PERMISSION)
    assert handler.kind_label() == "file sharing"


def test_display_name_returns_path(tmp_path: Path) -> None:
    handler, _sender = make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    assert handler.display_name_for_event(event) == "/home/user/data.txt"


# -- render_request_detail_fragment --


def test_render_request_detail_fragment_shows_path_and_rationale(tmp_path: Path) -> None:
    handler, _sender = make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/important.txt",
        access="READ",
        rationale="summarize the doc",
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="http://localhost:8421",
    )
    assert "/home/user/important.txt" in body
    assert "summarize the doc" in body
    assert "Approve" in body and "Deny" in body
    # The fragment must show the human-readable access label so the user
    # knows what's being granted.
    assert "read-only" in body
    # The fragment is right-pane-only and has no chrome of its own; the
    # inbox shell owns the backdrop, close button, and submission JS.
    assert "<html" not in body
    assert "permissions-backdrop" not in body
    assert "<script>" not in body


def test_render_request_detail_fragment_has_editable_path_and_browse(tmp_path: Path) -> None:
    """The dialog renders the path as an editable input plus a Browse button."""
    handler, _sender = make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/important.txt",
        access="READ",
        rationale="summarize the doc",
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="",
    )
    # The path is editable: an input named ``file_path`` pre-filled with
    # the requested path, plus separate file / folder pickers keyed for
    # the inbox shell.
    assert 'name="file_path"' in body
    assert 'id="file-sharing-path-input"' in body
    assert 'value="/home/user/important.txt"' in body
    assert 'id="file-sharing-browse-file-btn"' in body
    assert 'id="file-sharing-browse-folder-btn"' in body
    assert "browseForSharePath(&#39;file&#39;)" in body or "browseForSharePath('file')" in body
    assert "browseForSharePath(&#39;directory&#39;)" in body or "browseForSharePath('directory')" in body


def test_render_request_detail_fragment_marks_write_grants_distinctly(tmp_path: Path) -> None:
    """WRITE grants render the broader human-readable access label."""
    handler, _sender = make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/edit.txt",
        access="WRITE",
        rationale="edit it",
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="",
    )
    assert "read &amp; write" in body or "read & write" in body
    # The read-only label must not appear when WRITE access is being
    # requested, otherwise the fragment would be misleading.
    assert "read-only" not in body


def test_render_request_detail_fragment_escapes_html_in_inputs(tmp_path: Path) -> None:
    handler, _sender = make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    # A path crafted to break out of the value="" attribute and inject an
    # event handler if the renderer naively interpolated it.
    malicious_path = '/tmp/" onfocus="alert(1)'
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path=malicious_path,
        access="READ",
        rationale="<img src=x onerror=alert(2)>",
    )
    body = str(
        handler.render_request_detail_fragment(
            req_event=event,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            mngr_forward_origin="",
        )
    )
    # The rationale is rendered as element text, so raw HTML must be
    # entity-escaped.
    assert "<img src=x" not in body

    # The path is rendered into the value="" attribute of the editable
    # input. Parse the input and verify the attribute carries the path
    # verbatim with no injected handler -- i.e. the renderer safely quoted
    # the value rather than letting the crafted ``"`` break out.
    attrs = _parse_input_attrs(body, element_id="file-sharing-path-input")
    assert attrs["value"] == malicious_path
    assert "onfocus" not in attrs


def test_render_request_detail_fragment_embeds_allowed_roots(tmp_path: Path) -> None:
    """The dialog embeds the share roots so the inbox shell can validate client-side."""
    handler, _sender = make_file_sharing_handler(
        tmp_path, lambda r: httpx.Response(200), share_roots=(Path("/home/glenn"), Path("/tmp"))
    )
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/glenn/notes.txt",
        access="READ",
        rationale="r",
    )
    body = str(
        handler.render_request_detail_fragment(
            req_event=event,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            mngr_forward_origin="",
        )
    )
    attrs = _parse_input_attrs(body, element_id="file-sharing-path-input")
    assert json.loads(attrs["data-allowed-roots"]) == ["/home/glenn", "/tmp"]
