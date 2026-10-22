import json
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.testing import build_ui_state_publisher_for_test
from imbue.minds.desktop_client.testing import drain_ui_channel_frames
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiOpenHelpMessage
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.ui_models import UiWorkspaceRefreshMessage
from imbue.minds.desktop_client.ui_models import UiWorkspaceStoppedMessage
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage


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


def test_publish_now_broadcasts_every_message_type_on_first_pass() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)

    publisher.publish_now()

    types = [frame["type"] for frame in drain_ui_channel_frames(client_queue)]
    assert sorted(types) == [
        "accounts",
        "discovery_health",
        "environment",
        "notifications",
        "providers",
        "requests",
        "workspace_updates",
        "workspaces",
    ]


def test_publish_now_suppresses_unchanged_frames_on_second_pass() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)
    publisher.publish_now()
    drain_ui_channel_frames(client_queue)

    publisher.publish_now()

    assert drain_ui_channel_frames(client_queue) == []


def test_publish_now_rebroadcasts_only_the_changed_message_type() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)
    publisher.publish_now()
    drain_ui_channel_frames(client_queue)

    source.entries = (UiWorkspaceEntry(id="agent-1", name="one", accent="#112233"),)
    publisher.publish_now()

    frames = drain_ui_channel_frames(client_queue)
    assert [frame["type"] for frame in frames] == ["workspaces"]
    # Re-parse the single frame through the typed wire model rather than
    # spelunking untyped JSON: the assertion is that the broadcast frame
    # round-trips to the entry the source provided.
    parsed = UiWorkspacesMessage.model_validate_json(json.dumps(frames[0]))
    assert [entry.name for entry in parsed.workspaces] == ["one"]


def test_one_shot_workspace_stopped_event_reaches_the_channel() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)

    publisher.publish_one_shot(UiWorkspaceStoppedMessage(agent_id="agent-42"))

    frames = drain_ui_channel_frames(client_queue)
    assert frames == [{"type": "workspace_stopped", "agent_id": "agent-42"}]


def test_one_shot_open_help_event_reaches_the_channel() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)

    publisher.publish_one_shot(UiOpenHelpMessage(description="the diagnosis", workspace_agent_id="agent-7"))

    frames = drain_ui_channel_frames(client_queue)
    assert frames == [{"type": "open_help", "description": "the diagnosis", "workspace_agent_id": "agent-7"}]


def test_one_shot_workspace_refresh_event_reaches_the_channel() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)

    publisher.publish_one_shot(UiWorkspaceRefreshMessage(agent_id="agent-13"))

    frames = drain_ui_channel_frames(client_queue)
    assert frames == [{"type": "workspace_refresh", "agent_id": "agent-13", "loaded_before": None}]


def test_publish_health_broadcasts_immediately_without_diffing() -> None:
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)

    publisher.publish_health(UiHealthMessage(agent_id="agent-9", status=AgentHealth.STUCK, error=None))
    publisher.publish_health(UiHealthMessage(agent_id="agent-9", status=AgentHealth.STUCK, error=None))

    frames = drain_ui_channel_frames(client_queue)
    assert [frame["type"] for frame in frames] == ["health", "health"]


def _wait_for_frame(client_queue: "queue.Queue[str | None]", is_wanted: Callable[[dict[str, Any]], bool]) -> bool:
    """Block on the client queue (no polling) until a frame satisfying ``is_wanted`` arrives, or 5s pass."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            item = client_queue.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            return False
        if item is not None and is_wanted(json.loads(item)):
            return True
    return False


def test_publish_loop_survives_an_unexpected_derive_exception() -> None:
    """The publisher strand is the only source of chrome state, so an
    unexpected exception in one pass must not kill the loop: later wakes must
    still publish."""
    source = _MutableWorkspaceSource()
    did_explode = threading.Event()

    def derive_that_explodes_once() -> UiWorkspacesMessage:
        if not did_explode.is_set():
            did_explode.set()
            raise KeyError("an unexpected derive bug")
        return source.derive()

    exploding_publisher, client_queue = build_ui_state_publisher_for_test(derive_that_explodes_once)
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
        # The surviving loop's second pass must broadcast the workspaces frame.
        is_published = _wait_for_frame(client_queue, lambda frame: frame["type"] == "workspaces")
        exploding_publisher.stop()
    assert is_published


def _workspace_entry_named(name: str) -> UiWorkspaceEntry:
    return UiWorkspaceEntry(id=f"agent-{name}", name=name, accent="#112233")


def test_publish_loop_spaces_back_to_back_wakes_by_the_minimum_gap() -> None:
    """A producer firing again as soon as a pass starts still gets at most one pass per gap."""
    source = _MutableWorkspaceSource()
    pass_start_queue: "queue.Queue[float]" = queue.Queue()

    def derive_recording_pass_start() -> UiWorkspacesMessage:
        pass_start_queue.put(time.monotonic())
        return source.derive()

    min_gap_seconds = 0.2
    publisher, _client_queue = build_ui_state_publisher_for_test(
        derive_recording_pass_start, min_seconds_between_passes=min_gap_seconds
    )
    pass_starts: list[float] = []
    concurrency_group = ConcurrencyGroup(name="test-ui-publisher-gap")
    with concurrency_group:
        publisher.start(concurrency_group)
        for idx in range(4):
            source.entries = (_workspace_entry_named(f"pass-{idx}"),)
            publisher.notify_change()
            pass_starts.append(pass_start_queue.get(timeout=5.0))
        publisher.stop()

    gaps = [later - earlier for earlier, later in zip(pass_starts, pass_starts[1:], strict=False)]
    # The gap is enforced between pass starts; the derive runs a hair after
    # its pass starts, so allow that jitter.
    assert all(gap >= min_gap_seconds * 0.9 for gap in gaps), gaps


def test_publish_loop_publishes_the_last_change_of_a_burst() -> None:
    """Wakes folded into one pass are not lost: the state after the burst's last change goes out."""
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive, min_seconds_between_passes=0.3)
    concurrency_group = ConcurrencyGroup(name="test-ui-publisher-burst")
    with concurrency_group:
        publisher.start(concurrency_group)
        for idx in range(50):
            source.entries = (_workspace_entry_named(f"burst-{idx}"),)
            publisher.notify_change()
        is_last_change_published = _wait_for_frame(
            client_queue,
            lambda frame: (
                frame["type"] == "workspaces" and [entry["name"] for entry in frame["workspaces"]] == ["burst-49"]
            ),
        )
        publisher.stop()
    assert is_last_change_published


def test_replayed_health_frames_say_they_are_a_replay() -> None:
    """A snapshot health frame is marked; the same state published live is not.

    The SPA raises the recovery card on the edge into RECOVERY_FAILED, and a
    failure that was already there when the window connected is not an edge. The
    frame has to say which it is: nothing about the connect sequence's shape is
    promised, so a client inferring it from position would go on inferring it
    after any reordering, silently.
    """
    failed = UiHealthMessage(agent_id="agent-ab12", status=AgentHealth.RECOVERY_FAILED, error="no answer")
    publisher, client_queue = build_ui_state_publisher_for_test(
        _MutableWorkspaceSource().derive, derive_health_states=lambda: (failed,)
    )

    snapshot_health = [
        frame for frame in (json.loads(raw) for raw in publisher.build_snapshot_frames()) if frame["type"] == "health"
    ]
    publisher.publish_health(failed)

    assert [frame["is_snapshot"] for frame in snapshot_health] == [True]
    assert [frame["is_snapshot"] for frame in drain_ui_channel_frames(client_queue)] == [False]


def test_snapshot_frames_start_with_hello_and_cover_every_snapshot_type() -> None:
    source = _MutableWorkspaceSource()
    publisher, _client_queue = build_ui_state_publisher_for_test(source.derive)

    frames = [json.loads(frame) for frame in publisher.build_snapshot_frames()]

    assert frames[0]["type"] == "hello"
    assert frames[0]["schema_version"] == UI_SCHEMA_VERSION
    assert [frame["type"] for frame in frames[1:]] == [
        "workspaces",
        "accounts",
        "providers",
        "requests",
        "discovery_health",
        "notifications",
        "workspace_updates",
        "environment",
    ]


def test_snapshot_notifications_frame_says_it_is_a_replay() -> None:
    """Same contract as health: the snapshot's notifications frame is marked, a live publish is not."""
    source = _MutableWorkspaceSource()
    publisher, client_queue = build_ui_state_publisher_for_test(source.derive)

    snapshot_frames = [json.loads(frame) for frame in publisher.build_snapshot_frames()]
    publisher.publish_now()

    snapshot_notifications = [frame for frame in snapshot_frames if frame["type"] == "notifications"]
    assert [frame["is_snapshot"] for frame in snapshot_notifications] == [True]
    live_notifications = [frame for frame in drain_ui_channel_frames(client_queue) if frame["type"] == "notifications"]
    assert [frame["is_snapshot"] for frame in live_notifications] == [False]
