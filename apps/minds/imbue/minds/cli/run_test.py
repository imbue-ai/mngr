"""Unit tests for ``minds run`` helpers (the
``_StreamedPermissionRequestHandler`` private class and the mngr-host-dir
resolution).
"""

from pathlib import Path

from fastapi import FastAPI

from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.cli.run import _StreamedPermissionRequestHandler
from imbue.minds.cli.run import _resolve_mngr_host_dir
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event

# -- _StreamedPermissionRequestHandler -------------------------------------


def _make_streamed_permission_handler() -> tuple[
    _StreamedPermissionRequestHandler, FastAPI, MngrCliBackendResolver, list[int]
]:
    """Build a handler against a real FastAPI + resolver, plus a notify-count list.

    The list grows by one each time ``backend_resolver.notify_change()``
    fires; tests assert on its length to verify the dedup-or-not
    behaviour without poking the resolver's internals.
    """
    app = FastAPI()
    app.state.request_inbox = RequestInbox()
    resolver = MngrCliBackendResolver()
    notify_counts: list[int] = []
    resolver.add_on_change_callback(lambda: notify_counts.append(1))
    handler = _StreamedPermissionRequestHandler(app=app, backend_resolver=resolver)
    return handler, app, resolver, notify_counts


def test_streamed_permission_handler_records_first_delivery() -> None:
    handler, app, _, notify_counts = _make_streamed_permission_handler()
    event = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )

    handler(event)

    inbox = app.state.request_inbox
    assert isinstance(inbox, RequestInbox)
    assert len(inbox.requests) == 1
    assert str(inbox.requests[0].event_id) == str(event.event_id)
    assert len(notify_counts) == 1


def test_streamed_permission_handler_dedupes_redelivery_by_event_id() -> None:
    """The gateway re-emits pending requests on every reconnect; redeliveries must be no-ops.

    Without the dedup guard the requests list would grow unbounded
    across reconnects, the log would emit duplicate INFO lines, and
    the chrome SSE would wake up repeatedly for no reason.
    """
    handler, app, _, notify_counts = _make_streamed_permission_handler()
    event = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )

    for _ in range(5):
        handler(event)

    inbox = app.state.request_inbox
    assert isinstance(inbox, RequestInbox)
    # Only the first delivery appended; the subsequent four were
    # recognized as redeliveries and skipped.
    assert len(inbox.requests) == 1
    assert len(notify_counts) == 1


def test_streamed_permission_handler_records_distinct_events() -> None:
    """Different ``event_id``s are distinct requests even if other fields collide."""
    handler, app, _, notify_counts = _make_streamed_permission_handler()
    first = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )
    second = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )
    assert first.event_id != second.event_id

    handler(first)
    handler(second)

    inbox = app.state.request_inbox
    assert isinstance(inbox, RequestInbox)
    assert len(inbox.requests) == 2
    assert len(notify_counts) == 2


def test_streamed_permission_handler_noop_when_inbox_not_initialised() -> None:
    """If ``state.request_inbox`` is ``None`` (boot order), the handler silently no-ops."""
    app = FastAPI()
    app.state.request_inbox = None
    resolver = MngrCliBackendResolver()
    notify_counts: list[int] = []
    resolver.add_on_change_callback(lambda: notify_counts.append(1))
    handler = _StreamedPermissionRequestHandler(app=app, backend_resolver=resolver)
    event = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )

    handler(event)

    assert app.state.request_inbox is None
    assert notify_counts == []


# -- _resolve_mngr_host_dir ------------------------------------------------


def test_resolve_mngr_host_dir_honors_explicit_override() -> None:
    """A set MNGR_HOST_DIR is used verbatim (with ~ expansion), ignoring root_name."""
    resolved = _resolve_mngr_host_dir("/custom/mngr/host", "minds")
    assert resolved == Path("/custom/mngr/host")


def test_resolve_mngr_host_dir_expands_user_in_override() -> None:
    resolved = _resolve_mngr_host_dir("~/explicit-mngr", "minds")
    assert resolved == Path.home() / "explicit-mngr"


def test_resolve_mngr_host_dir_falls_back_to_root_name_derived_dir() -> None:
    """When MNGR_HOST_DIR is unset the fallback is the root-name-derived dir, not ~/.mngr."""
    fallback = _resolve_mngr_host_dir(None, "minds")
    assert fallback == mngr_host_dir_for("minds")
    # Specifically NOT mngr's bare default, which would point `mngr forward` at
    # a different agent topology than the rest of `run` reads.
    assert fallback != Path.home() / ".mngr"


def test_resolve_mngr_host_dir_empty_string_is_treated_as_unset() -> None:
    assert _resolve_mngr_host_dir("", "dev-alice") == mngr_host_dir_for("dev-alice")
