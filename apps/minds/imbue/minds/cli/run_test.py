"""Unit tests for ``minds run`` helpers (currently the
``_StreamedPermissionRequestHandler`` private class).
"""

from fastapi import FastAPI

from imbue.minds.cli.run import _StreamedPermissionRequestHandler
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
