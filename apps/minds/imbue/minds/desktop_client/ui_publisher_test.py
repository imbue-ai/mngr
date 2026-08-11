import json
import queue
import threading
import time

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.chrome_event_broadcast import ChromeEventBroadcaster
from imbue.minds.desktop_client.chrome_event_broadcast import build_open_help_payload
from imbue.minds.desktop_client.chrome_event_broadcast import build_workspace_refresh_payload
from imbue.minds.desktop_client.chrome_event_broadcast import build_workspace_stopped_payload
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.ui_channel import UiChannelBroadcaster
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiDiscoveryHealthMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.desktop_client.ui_publisher import UiStatePublisher


class _MutableWorkspaceSource:
    """A tiny stand-in for the resolver-backed derive: tests mutate ``entries`` between passes."""

    def __init__(self) -> None:
        self.entries: tuple[UiWorkspaceEntry, ...] = ()

    def derive(self) -> UiWorkspacesMessage:
        return UiWorkspacesMessage(
            workspaces=self.entries,
            destroying_agent_ids=(),
            restorable_workspace_ids=(),
            remote_workspace_states={},
        )


def _build_publisher(source: _MutableWorkspaceSource) -> tuple[UiStatePublisher, "queue.Queue[str | None]"]:
    broadcaster = UiChannelBroadcaster()
    publisher = UiStatePublisher(
        broadcaster=broadcaster,
        derive_workspaces=source.derive,
        derive_accounts=lambda: UiAccountsMessage(has_accounts=False, account_email="", extra_account_count=0),
        derive_providers=lambda: UiProvidersMessage(providers=(), last_event_at=None, last_full_snapshot_at=None),
        derive_requests=lambda: UiRequestsMessage(count=0, request_ids=(), auto_open=True),
        derive_discovery_health=lambda: UiDiscoveryHealthMessage(state=DiscoveryHealth.HEALTHY),
        derive_health_states=lambda: (),
    )
    client_queue = broadcaster.register()
    return publisher, client_queue


def _drain_frames(client_queue: "queue.Queue[str | None]") -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    is_drained = False
    while not is_drained:
        try:
            item = client_queue.get_nowait()
        except queue.Empty:
            is_drained = True
            continue
        assert item is not None
        frames.append(json.loads(item))
    return frames


def test_publish_now_broadcasts_every_message_type_on_first_pass() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)

    publisher.publish_now()

    types = [frame["type"] for frame in _drain_frames(client_queue)]
    assert sorted(types) == ["accounts", "discovery_health", "providers", "requests", "workspaces"]


def test_publish_now_suppresses_unchanged_frames_on_second_pass() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)
    publisher.publish_now()
    _drain_frames(client_queue)

    publisher.publish_now()

    assert _drain_frames(client_queue) == []


def test_publish_now_rebroadcasts_only_the_changed_message_type() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)
    publisher.publish_now()
    _drain_frames(client_queue)

    source.entries = (UiWorkspaceEntry(id="agent-1", name="one", accent="#112233"),)
    publisher.publish_now()

    frames = _drain_frames(client_queue)
    assert [frame["type"] for frame in frames] == ["workspaces"]
    # Re-parse the single frame through the typed wire model rather than
    # spelunking untyped JSON: the assertion is that the broadcast frame
    # round-trips to the entry the source provided.
    parsed = UiWorkspacesMessage.model_validate_json(json.dumps(frames[0]))
    assert [entry.name for entry in parsed.workspaces] == ["one"]


def test_bridged_legacy_workspace_stopped_event_reaches_the_channel() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)
    broker = ChromeEventBroadcaster()
    publisher.bridge_legacy_broker(broker)

    broker.broadcast(build_workspace_stopped_payload("agent-42"))
    publisher.publish_now()

    frames = _drain_frames(client_queue)
    stopped = [frame for frame in frames if frame["type"] == "workspace_stopped"]
    assert stopped == [{"type": "workspace_stopped", "agent_id": "agent-42"}]


def test_bridged_legacy_open_help_event_reaches_the_channel() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)
    broker = ChromeEventBroadcaster()
    publisher.bridge_legacy_broker(broker)

    broker.broadcast(build_open_help_payload("the diagnosis", "agent-7"))
    publisher.publish_now()

    frames = _drain_frames(client_queue)
    help_frames = [frame for frame in frames if frame["type"] == "open_help"]
    assert help_frames == [{"type": "open_help", "description": "the diagnosis", "workspace_agent_id": "agent-7"}]


def test_bridged_legacy_workspace_refresh_event_reaches_the_channel() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)
    broker = ChromeEventBroadcaster()
    publisher.bridge_legacy_broker(broker)

    broker.broadcast(build_workspace_refresh_payload("agent-13"))
    publisher.publish_now()

    frames = _drain_frames(client_queue)
    refreshes = [frame for frame in frames if frame["type"] == "workspace_refresh"]
    assert refreshes == [{"type": "workspace_refresh", "agent_id": "agent-13"}]


def test_publish_health_broadcasts_immediately_without_diffing() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)

    publisher.publish_health(UiHealthMessage(agent_id="agent-9", status=AgentHealth.STUCK, error=None))
    publisher.publish_health(UiHealthMessage(agent_id="agent-9", status=AgentHealth.STUCK, error=None))

    frames = _drain_frames(client_queue)
    assert [frame["type"] for frame in frames] == ["health", "health"]


def test_publish_loop_survives_an_unexpected_derive_exception() -> None:
    """The publisher strand is the only source of chrome state, so an
    unexpected exception in one pass must not kill the loop: later wakes must
    still publish."""
    source = _MutableWorkspaceSource()
    publisher, client_queue = _build_publisher(source)
    original_derive = source.derive
    did_explode = threading.Event()

    def derive_that_explodes_once() -> UiWorkspacesMessage:
        if not did_explode.is_set():
            did_explode.set()
            raise KeyError("an unexpected derive bug")
        return original_derive()

    # Rebuild the publisher around the exploding derive (fields are frozen).
    exploding_publisher = UiStatePublisher(
        broadcaster=publisher.broadcaster,
        derive_workspaces=derive_that_explodes_once,
        derive_accounts=publisher.derive_accounts,
        derive_providers=publisher.derive_providers,
        derive_requests=publisher.derive_requests,
        derive_discovery_health=publisher.derive_discovery_health,
        derive_health_states=publisher.derive_health_states,
    )
    concurrency_group = ConcurrencyGroup(name="test-ui-publisher")
    with concurrency_group:
        exploding_publisher.start(concurrency_group)
        # First wake explodes (KeyError is outside publish_now's narrow derive
        # handling); the loop must survive it.
        exploding_publisher.notify_change()
        assert did_explode.wait(timeout=5.0)
        # This wake lands during or after the exploding pass: the loop clears
        # the event BEFORE publishing, so it always yields another pass.
        exploding_publisher.notify_change()
        # Block on the client queue (no polling): the surviving loop's second
        # pass must broadcast the workspaces frame.
        is_published = False
        deadline = time.monotonic() + 5.0
        while not is_published and time.monotonic() < deadline:
            try:
                item = client_queue.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            is_published = item is not None and json.loads(item)["type"] == "workspaces"
        exploding_publisher.stop()
    assert is_published


def test_snapshot_frames_start_with_hello_and_cover_every_snapshot_type() -> None:
    source = _MutableWorkspaceSource()
    publisher, _client_queue = _build_publisher(source)

    frames = [json.loads(frame) for frame in publisher.build_snapshot_frames()]

    assert frames[0]["type"] == "hello"
    assert frames[0]["schema_version"] == 1
    assert [frame["type"] for frame in frames[1:]] == [
        "workspaces",
        "accounts",
        "providers",
        "requests",
        "discovery_health",
    ]
