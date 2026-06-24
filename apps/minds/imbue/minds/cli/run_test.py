"""Unit tests for ``minds run`` helpers (currently the
``_StreamedPermissionRequestHandler`` private class).
"""

from pathlib import Path

from flask import Flask

from imbue.imbue_common.model_update import to_update
from imbue.minds.cli.run import _StreamedPermissionRequestHandler
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.state import set_state
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_latchkey.agent_setup import prepare_agent_latchkey
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.testing import FakeLatchkey
from imbue.mngr_latchkey.testing import make_full_fake_latchkey

# -- _StreamedPermissionRequestHandler -------------------------------------


def _make_app_with_inbox(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    inbox: RequestInbox | None,
) -> Flask:
    """Build a Flask app carrying a desktop-client state with the given inbox + resolver."""
    app = Flask(__name__)
    set_state(
        app,
        DesktopClientState(
            auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
            backend_resolver=resolver,
            request_inbox=inbox,
        ),
    )
    return app


def _make_streamed_permission_handler(
    tmp_path: Path,
    latchkey: FakeLatchkey | None = None,
) -> tuple[_StreamedPermissionRequestHandler, Flask, MngrCliBackendResolver, list[int]]:
    """Build a handler against a real Flask app + resolver, plus a notify-count list.

    The list grows by one each time ``backend_resolver.notify_change()``
    fires; tests assert on its length to verify the dedup-or-not
    behaviour without poking the resolver's internals.
    """
    resolver = MngrCliBackendResolver()
    app = _make_app_with_inbox(tmp_path, resolver, RequestInbox())
    notify_counts: list[int] = []
    resolver.add_on_change_callback(lambda: notify_counts.append(1))
    handler = _StreamedPermissionRequestHandler(
        app=app,
        backend_resolver=resolver,
        latchkey=latchkey if latchkey is not None else make_full_fake_latchkey(tmp_path),
    )
    return handler, app, resolver, notify_counts


def test_streamed_permission_handler_records_first_delivery(tmp_path: Path) -> None:
    handler, app, _, notify_counts = _make_streamed_permission_handler(tmp_path)
    event = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )

    handler(event)

    inbox = get_state(app).request_inbox
    assert isinstance(inbox, RequestInbox)
    assert len(inbox.requests) == 1
    assert str(inbox.requests[0].event_id) == str(event.event_id)
    assert len(notify_counts) == 1


def test_streamed_permission_handler_dedupes_redelivery_by_event_id(tmp_path: Path) -> None:
    """The gateway re-emits pending requests on every reconnect; redeliveries must be no-ops.

    Without the dedup guard the requests list would grow unbounded
    across reconnects, the log would emit duplicate INFO lines, and
    the chrome SSE would wake up repeatedly for no reason.
    """
    handler, app, _, notify_counts = _make_streamed_permission_handler(tmp_path)
    event = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )

    for _ in range(5):
        handler(event)

    inbox = get_state(app).request_inbox
    assert isinstance(inbox, RequestInbox)
    # Only the first delivery appended; the subsequent four were
    # recognized as redeliveries and skipped.
    assert len(inbox.requests) == 1
    assert len(notify_counts) == 1


def test_streamed_permission_handler_records_distinct_events(tmp_path: Path) -> None:
    """Different ``event_id``s are distinct requests even if other fields collide."""
    handler, app, _, notify_counts = _make_streamed_permission_handler(tmp_path)
    first = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )
    second = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )
    assert first.event_id != second.event_id

    handler(first)
    handler(second)

    inbox = get_state(app).request_inbox
    assert isinstance(inbox, RequestInbox)
    assert len(inbox.requests) == 2
    assert len(notify_counts) == 2


def test_streamed_permission_handler_noop_when_inbox_not_initialised(tmp_path: Path) -> None:
    """If ``state.request_inbox`` is ``None`` (boot order), the handler silently no-ops."""
    resolver = MngrCliBackendResolver()
    app = _make_app_with_inbox(tmp_path, resolver, None)
    notify_counts: list[int] = []
    resolver.add_on_change_callback(lambda: notify_counts.append(1))
    handler = _StreamedPermissionRequestHandler(
        app=app, backend_resolver=resolver, latchkey=make_full_fake_latchkey(tmp_path)
    )
    event = create_latchkey_predefined_permission_request_event(
        agent_id="agent-abc", scope="slack-api", rationale="why"
    )

    handler(event)

    assert get_state(app).request_inbox is None
    assert notify_counts == []


def test_streamed_permission_handler_recovers_missing_host_permissions(tmp_path: Path) -> None:
    """A first request for a host whose canonical permissions file is missing repairs it.

    Reproduces the production failure mode: agent creation's finalize/link step
    never created ``hosts/<host_id>/latchkey_permissions.json``, but the agent
    is live and files a permission request carrying its opaque handle as the
    ``permissions_target_path``. Surfacing the request must also materialize the
    canonical file (swinging the opaque handle's symlink onto it) so the user's
    eventual approval is visible to the agent.
    """
    latchkey = make_full_fake_latchkey(tmp_path)
    # Stand up the opaque baseline handle exactly as agent creation does, but
    # deliberately skip the finalize/link step that would create the host file.
    setup = prepare_agent_latchkey(latchkey, is_tunneled=True)
    assert setup.opaque_permissions_path is not None

    host_id = HostId()
    agent_id = AgentId()
    handler, app, resolver, _ = _make_streamed_permission_handler(tmp_path, latchkey=latchkey)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent_id,),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=agent_id,
                    agent_name=AgentName("agent"),
                    provider_name=ProviderInstanceName("local"),
                ),
            ),
        )
    )

    canonical = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    assert not canonical.exists()

    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id), scope="slack-api", rationale="why"
    )
    event = event.model_copy_update(
        to_update(event.field_ref().permissions_target_path, str(setup.opaque_permissions_path))
    )

    handler(event)

    # The canonical file now exists and the opaque handle is a symlink to it.
    assert canonical.is_file()
    assert setup.opaque_permissions_path.is_symlink()
    assert setup.opaque_permissions_path.resolve() == canonical.resolve()
    # The requesting agent was registered into the host's allowlist.
    assert str(agent_id) in canonical.read_text()
    # The request was still surfaced to the inbox.
    inbox = get_state(app).request_inbox
    assert isinstance(inbox, RequestInbox)
    assert len(inbox.requests) == 1
