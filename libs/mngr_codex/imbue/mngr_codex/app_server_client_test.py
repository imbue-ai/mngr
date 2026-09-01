"""Unit tests for :mod:`imbue.mngr_codex.app_server_client`.

Every path is driven against a scripted in-memory transport (constructor injection),
so no live ``codex app-server`` daemon is needed. The transport records the frames the
client sends and, per configured method, answers with a JSON-RPC response and/or pushes
notifications -- letting each test script the exact daemon behavior a scenario needs.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import Disposition
from imbue.mngr_codex.app_server_client import DispositionKind
from imbue.mngr_codex.app_server_client import ThreadInfo
from imbue.mngr_codex.app_server_client import TransportClosedError
from imbue.mngr_codex.app_server_client import UNSET
from imbue.mngr_codex.app_server_client import WebsocketAppServerTransport


class ScriptedTransport:
    """An in-memory transport double whose responses tests configure per method.

    ``respond_result`` / ``respond_error`` register how a request method is answered;
    ``respond`` registers a custom handler (for stateful or notification-emitting
    behavior). ``push`` feeds an arbitrary inbound frame (e.g. a notification). Sent
    frames are recorded in ``sent`` (raw) and readable parsed via :meth:`sent_of`.
    """

    def __init__(self) -> None:
        self._inbound: deque[str] = deque()
        self.sent: list[str] = []
        self._responders: dict[str, Callable[[Mapping[str, Any]], None]] = {}
        self._is_closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)
        request = json.loads(message)
        responder = self._responders.get(request.get("method"))
        if responder is not None:
            responder(request)

    def receive(self, timeout: float | None) -> str:
        if self._is_closed:
            raise TransportClosedError("scripted transport closed")
        if not self._inbound:
            raise TimeoutError("no frame available")
        return self._inbound.popleft()

    def close(self) -> None:
        self._is_closed = True

    def push(self, frame: Mapping[str, Any]) -> None:
        self._inbound.append(json.dumps(frame))

    def respond(self, method: str, responder: Callable[[Mapping[str, Any]], None]) -> None:
        self._responders[method] = responder

    def respond_result(self, method: str, result: Mapping[str, Any]) -> None:
        self.respond(method, lambda request: self.push({"jsonrpc": "2.0", "id": request["id"], "result": result}))

    def respond_error(self, method: str, code: int, message: str) -> None:
        self.respond(
            method,
            lambda request: self.push(
                {"jsonrpc": "2.0", "id": request["id"], "error": {"code": code, "message": message}}
            ),
        )

    def sent_of(self, method: str) -> list[Mapping[str, Any]]:
        parsed = [json.loads(frame) for frame in self.sent]
        return [frame for frame in parsed if frame.get("method") == method]


def _initialize_result() -> dict[str, Any]:
    return {
        "userAgent": "mngr/0.147.0",
        "codexHome": "/home/agent/.codex",
        "platformFamily": "unix",
        "platformOs": "linux",
    }


def _thread_start_result(thread_id: str = "thread-1", status_type: str = "idle") -> dict[str, Any]:
    return {
        "thread": {"id": thread_id, "status": {"type": status_type}},
        "model": "gpt-5.6-sol",
        "reasoningEffort": "medium",
        "serviceTier": None,
    }


def _handshaken_client(transport: ScriptedTransport) -> CodexAppServerClient:
    """Return a client that has completed ``initialize`` and bound a thread."""
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/start", _thread_start_result())
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    client.thread_start(cwd="/work")
    return client


# =============================================================================
# handshake
# =============================================================================


def test_initialize_handshake_sends_experimental_capability_and_initialized() -> None:
    transport = ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    client = CodexAppServerClient(transport=transport)
    result = client.initialize("mngr", "0.1")
    assert result.codex_home == "/home/agent/.codex"
    assert result.platform_os == "linux"
    initialize_frames = transport.sent_of("initialize")
    assert len(initialize_frames) == 1
    assert initialize_frames[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert initialize_frames[0]["params"]["clientInfo"] == {"name": "mngr", "version": "0.1"}
    initialized_frames = transport.sent_of("initialized")
    assert len(initialized_frames) == 1
    assert "id" not in initialized_frames[0]


def test_thread_start_binds_thread_and_returns_seed() -> None:
    transport = ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/start", _thread_start_result(thread_id="thread-xyz"))
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    info = client.thread_start(cwd="/work", model="gpt-5.6-sol")
    assert client.thread_id == "thread-xyz"
    assert info.thread_id == "thread-xyz"
    assert info.model == "gpt-5.6-sol"
    assert info.effort == "medium"
    assert info.status == {"type": "idle"}
    assert transport.sent_of("thread/start")[0]["params"] == {"cwd": "/work", "model": "gpt-5.6-sol"}
    # The seed is captured so a caller that binds via the opener can seed the model bar without a
    # second RPC (system_interface's live connection uses this on connect).
    assert client.last_thread_info is info


def test_inject_items_materializes_bound_thread_without_a_turn() -> None:
    """``inject_items`` sends ``thread/inject_items`` for the bound thread and runs no turn.

    This is the create-time materialize step: it writes the rollout (so ``codex resume`` can
    cold-load it) with no model call. It threads the bound ``threadId`` and the caller's items,
    and never emits a ``turn/*`` request.
    """
    transport = ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/start", _thread_start_result(thread_id="root-1"))
    transport.respond_result("thread/inject_items", {})
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    client.thread_start(cwd="/work")
    client.inject_items(({"type": "environmentContext", "text": ""},))
    inject_frames = transport.sent_of("thread/inject_items")
    assert len(inject_frames) == 1
    assert inject_frames[0]["params"] == {
        "threadId": "root-1",
        "items": [{"type": "environmentContext", "text": ""}],
    }
    assert transport.sent_of("turn/start") == []
    assert transport.sent_of("turn/steer") == []


def test_inject_items_raises_without_a_bound_thread() -> None:
    transport = ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    with pytest.raises(CodexAppServerError):
        client.inject_items(({"type": "environmentContext", "text": ""},))


def test_thread_resume_raises_on_missing_rollout() -> None:
    transport = ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_error("thread/resume", -32600, "no rollout found for thread id")
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    with pytest.raises(CodexAppServerError) as exc_info:
        client.thread_resume("thread-gone")
    assert exc_info.value.code == -32600


# =============================================================================
# submit: idle -> started, busy -> steered
# =============================================================================


def test_submit_when_idle_starts_a_turn() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    disposition = client.submit("hello", "cid-1")
    assert disposition == Disposition(kind=DispositionKind.STARTED, turn_id="turn-1")
    assert client.active_turn_id == "turn-1"
    start_frames = transport.sent_of("turn/start")
    assert start_frames[0]["params"]["clientUserMessageId"] == "cid-1"
    assert start_frames[0]["params"]["input"] == [{"type": "text", "text": "hello"}]
    assert start_frames[0]["params"]["threadId"] == "thread-1"


def test_submit_when_busy_steers_the_running_turn() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    client.submit("first", "cid-1")
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    disposition = client.submit("second", "cid-2")
    assert disposition == Disposition(kind=DispositionKind.STEERED, turn_id="turn-1")
    steer_frames = transport.sent_of("turn/steer")
    assert steer_frames[0]["params"]["expectedTurnId"] == "turn-1"
    assert steer_frames[0]["params"]["clientUserMessageId"] == "cid-2"


def test_submit_requires_a_bound_thread() -> None:
    transport = ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    with pytest.raises(CodexAppServerError):
        client.submit("hello", "cid-1")


# =============================================================================
# submit: ABA -32600 re-decide-once
# =============================================================================


def test_submit_aba_redecides_as_start_when_turn_ended() -> None:
    """A steer against a turn that just ended (-32600) re-decides once as a fresh start."""
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    # Bind an active turn (no live successor) via a notification, then process it.
    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    client.poll_notifications()
    assert client.active_turn_id == "turn-1"
    transport.respond_error("turn/steer", -32600, "no active turn to steer")
    transport.respond_result("turn/start", {"turn": {"id": "turn-2", "status": "inProgress"}})
    disposition = client.submit("late", "cid-3")
    assert disposition == Disposition(kind=DispositionKind.STARTED, turn_id="turn-2")
    assert len(transport.sent_of("turn/steer")) == 1
    assert len(transport.sent_of("turn/start")) == 1


def test_submit_aba_resteers_against_the_successor_turn() -> None:
    """When a successor turn is already active, the -32600 retry steers that new turn."""
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    client.poll_notifications()

    steer_calls = {"count": 0}

    def steer_responder(request: Mapping[str, Any]) -> None:
        steer_calls["count"] += 1
        if steer_calls["count"] == 1:
            # The expected turn ended; a successor is already running. Deliver the
            # successor's turn/started BEFORE the error so the retry re-decides to it.
            transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-2"}}})
            transport.push(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32600, "message": "no active turn to steer"},
                }
            )
        else:
            transport.push({"jsonrpc": "2.0", "id": request["id"], "result": {"turnId": "turn-2"}})

    transport.respond("turn/steer", steer_responder)
    disposition = client.submit("late", "cid-4")
    assert disposition == Disposition(kind=DispositionKind.STEERED, turn_id="turn-2")
    steer_frames = transport.sent_of("turn/steer")
    assert len(steer_frames) == 2
    assert steer_frames[0]["params"]["expectedTurnId"] == "turn-1"
    assert steer_frames[1]["params"]["expectedTurnId"] == "turn-2"


def test_submit_reraises_a_non_aba_error() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    client.poll_notifications()
    transport.respond_error("turn/steer", -32000, "some other failure")
    with pytest.raises(CodexAppServerError) as exc_info:
        client.submit("x", "cid-5")
    assert exc_info.value.code == -32000
    assert len(transport.sent_of("turn/steer")) == 1


# =============================================================================
# interrupt / model_list / settings_update
# =============================================================================


def test_interrupt_sends_turn_interrupt_for_the_bound_thread() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("turn/interrupt", {})
    client.interrupt("turn-9")
    assert transport.sent_of("turn/interrupt")[0]["params"] == {"threadId": "thread-1", "turnId": "turn-9"}


def test_interrupt_nowait_sends_the_frame_without_reading_the_response() -> None:
    """Fire-and-forget interrupt: the request frame is written (with an id, for the bound thread), but
    the method returns WITHOUT consuming the response, which is left for a later read to drop."""
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("turn/interrupt", {})
    client.interrupt_nowait("turn-9")
    sent = transport.sent_of("turn/interrupt")[0]
    assert sent["params"] == {"threadId": "thread-1", "turnId": "turn-9"}
    assert "id" in sent, "turn/interrupt is a request, so it carries an id even fire-and-forget"
    # The id-matched response is still queued (never read by interrupt_nowait); a later poll drops it
    # harmlessly, since a response frame carries no ``method`` and so dispatches to no handler.
    client.poll_notifications()


def test_model_list_parses_the_data_envelope() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result(
        "model/list",
        {
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "displayName": "GPT-5.6 Sol",
                    "description": "",
                    "hidden": False,
                    "isDefault": True,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "fast"},
                        {"reasoningEffort": "high", "description": "slow"},
                    ],
                    "serviceTiers": [{"id": "priority", "name": "Priority", "description": ""}],
                    "defaultServiceTier": None,
                }
            ],
            "nextCursor": None,
        },
    )
    models = client.model_list()
    assert len(models) == 1
    model = models[0]
    assert model.id == "gpt-5.6-sol"
    assert model.is_default is True
    assert tuple(effort.reasoning_effort for effort in model.supported_reasoning_efforts) == ("low", "high")
    assert model.service_tiers[0].id == "priority"
    assert transport.sent_of("model/list")[0]["params"] == {"includeHidden": False}


def test_model_list_raises_without_a_data_array() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("model/list", {"models": []})
    with pytest.raises(CodexAppServerError):
        client.model_list()


def test_settings_update_omits_unchanged_and_clears_with_null() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("thread/settings/update", {})
    # Only ``model`` set; ``effort`` cleared with None; ``service_tier`` omitted.
    client.settings_update(model="gpt-5.5", effort=None)
    params = transport.sent_of("thread/settings/update")[0]["params"]
    assert params == {"threadId": "thread-1", "model": "gpt-5.5", "effort": None}
    assert "serviceTier" not in params


def test_settings_update_defaults_send_only_the_thread_id() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("thread/settings/update", {})
    client.settings_update()
    assert UNSET is not None
    assert transport.sent_of("thread/settings/update")[0]["params"] == {"threadId": "thread-1"}


# =============================================================================
# notifications / activity tracking / errors
# =============================================================================


def test_notification_handlers_receive_events_and_track_active_turn() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    received: list[str] = []
    client.add_notification_handler(lambda method, params: received.append(method))

    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    client.poll_notifications()
    assert client.active_turn_id == "turn-1"

    transport.push(
        {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "completed"}},
        }
    )
    client.poll_notifications()
    assert client.active_turn_id is None
    assert received == ["turn/started", "turn/completed"]


class _TimeoutRecordingTransport(ScriptedTransport):
    """A ScriptedTransport that records the ``timeout`` each ``receive`` call is given."""

    def __init__(self) -> None:
        super().__init__()
        self.receive_timeouts: list[float | None] = []

    def receive(self, timeout: float | None) -> str:
        self.receive_timeouts.append(timeout)
        return super().receive(timeout)


def test_poll_notifications_drains_buffered_frames_without_blocking_per_frame() -> None:
    """Only the FIRST frame waits up to the caller's ``timeout``; subsequent frames are drained
    non-blocking (timeout 0). Otherwise a heavily-streaming turn keeps the drain fed within
    ``timeout`` per frame and pins the frame lock for seconds, delaying a concurrent
    interrupt/steer past the "immediate" bar (contract A5)."""
    transport = _TimeoutRecordingTransport()
    client = _handshaken_client(transport)
    # drop the handshake's own reads
    transport.receive_timeouts.clear()
    for i in range(3):
        transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": f"t{i}"}}})

    client.poll_notifications(timeout=0.2)

    # 3 buffered frames + 1 terminating empty read; the first waits 0.2, the rest are non-blocking.
    assert transport.receive_timeouts[0] == 0.2
    assert all(timeout == 0.0 for timeout in transport.receive_timeouts[1:])


def test_request_error_is_raised_with_code() -> None:
    transport = ScriptedTransport()
    transport.respond_error("initialize", -32600, "requires experimentalApi capability")
    client = CodexAppServerClient(transport=transport)
    with pytest.raises(CodexAppServerError) as exc_info:
        client.initialize("mngr", "0.1")
    assert exc_info.value.code == -32600


def test_close_closes_the_transport() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    client.close()
    with pytest.raises(TransportClosedError):
        transport.receive(0.0)


def test_malformed_and_untyped_frames_are_ignored() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    seen: list[str] = []
    client.add_notification_handler(lambda method, params: seen.append(method))
    # A non-JSON frame, a JSON array (non-object), a frame without a method, an
    # item/completed (no turn), and a turn-bearing method that is neither started nor
    # completed -- all must be tolerated and not disturb the tracked active turn.
    transport._inbound.append("not json")
    transport._inbound.append(json.dumps([1, 2, 3]))
    transport.push({"foo": "bar"})
    transport.push({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "agentMessage"}}})
    transport.push({"jsonrpc": "2.0", "method": "turn/diff/updated", "params": {"turn": {"id": "turn-x"}}})
    client.poll_notifications()
    assert client.active_turn_id is None
    assert seen == ["item/completed", "turn/diff/updated"]


# =============================================================================
# thread_resume / thread_read / result-shape guards
# =============================================================================


def test_thread_resume_binds_and_returns_seed() -> None:
    transport = ScriptedTransport()
    transport.respond_result("initialize", _initialize_result())
    transport.respond_result("thread/resume", _thread_start_result(thread_id="thread-resumed"))
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    info = client.thread_resume("thread-resumed", cwd="/work")
    assert info.thread_id == "thread-resumed"
    assert client.thread_id == "thread-resumed"
    # The resume seed is captured for the model-bar seed-on-connect.
    assert client.last_thread_info is info
    assert transport.sent_of("thread/resume")[0]["params"] == {"threadId": "thread-resumed", "cwd": "/work"}


def test_thread_read_sends_thread_id_and_include_turns() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("thread/read", {"thread": {"id": "thread-1", "turns": []}})
    result = client.thread_read(include_turns=True)
    assert result == {"thread": {"id": "thread-1", "turns": []}}
    assert transport.sent_of("thread/read")[0]["params"] == {"threadId": "thread-1", "includeTurns": True}


def test_thread_info_from_response_requires_a_thread_id() -> None:
    with pytest.raises(CodexAppServerError):
        ThreadInfo.from_response({"model": "gpt-5.6-sol"})


def test_thread_loaded_list_returns_the_data_ids() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("thread/loaded/list", {"data": ["thread-1", "thread-2"], "nextCursor": None})
    assert client.thread_loaded_list() == ("thread-1", "thread-2")
    assert transport.sent_of("thread/loaded/list")[0]["params"] == {}


def test_thread_loaded_list_tolerates_a_missing_data_array() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("thread/loaded/list", {})
    assert client.thread_loaded_list() == ()


def test_bind_thread_sets_the_thread_id_without_an_rpc() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    before = list(transport.sent)
    client.bind_thread("thread-already-loaded")
    assert client.thread_id == "thread-already-loaded"
    # No request was issued -- binding is a pure local assignment.
    assert transport.sent == before


def test_read_thread_status_seeds_active_turn_when_busy() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result(
        "thread/read",
        {
            "thread": {
                "id": "thread-1",
                "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
                "turns": [
                    {"id": "turn-done", "status": "completed"},
                    {"id": "turn-live", "status": "inProgress"},
                ],
            }
        },
    )
    snapshot = client.read_thread_status()
    assert snapshot.status_type == "active"
    assert snapshot.active_flags == ("waitingOnApproval",)
    assert snapshot.active_turn_id == "turn-live"
    assert snapshot.is_blocked_on_input is True
    # The active turn is seeded so the next submit steers rather than opening a 2nd turn.
    assert client.active_turn_id == "turn-live"
    assert transport.sent_of("thread/read")[0]["params"] == {"threadId": "thread-1", "includeTurns": True}


def test_read_thread_status_when_idle_clears_active_turn() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("thread/read", {"thread": {"id": "thread-1", "status": {"type": "idle"}, "turns": []}})
    snapshot = client.read_thread_status()
    assert snapshot.status_type == "idle"
    assert snapshot.active_turn_id is None
    assert snapshot.is_blocked_on_input is False
    assert client.active_turn_id is None


def test_read_thread_status_retries_without_turns_for_unmaterialized_thread() -> None:
    """A thread with no first user message rejects ``includeTurns:true``; status is read anyway.

    The daemon errors ``includeTurns is unavailable before first user message`` (verified live);
    the client retries ``includeTurns:false`` and reports the idle status with no active turn.
    """
    transport = ScriptedTransport()
    client = _handshaken_client(transport)

    def _respond_thread_read(request: Mapping[str, Any]) -> None:
        include_turns = request["params"]["includeTurns"]
        if include_turns:
            transport.push(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {
                        "code": -32000,
                        "message": "thread abc is not materialized yet; includeTurns unavailable",
                    },
                }
            )
        else:
            transport.push(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"thread": {"id": "thread-1", "status": {"type": "idle"}}},
                }
            )

    transport.respond("thread/read", _respond_thread_read)
    snapshot = client.read_thread_status()
    assert snapshot.status_type == "idle"
    assert snapshot.active_turn_id is None
    assert client.active_turn_id is None
    reads = transport.sent_of("thread/read")
    assert [read["params"]["includeTurns"] for read in reads] == [True, False]


def test_read_thread_status_reraises_a_non_materialization_error() -> None:
    """A ``thread/read`` failure that is NOT the unmaterialized case is not retried -- it raises."""
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_error("thread/read", -32000, "internal daemon error")
    with pytest.raises(CodexAppServerError):
        client.read_thread_status()
    assert len(transport.sent_of("thread/read")) == 1


def test_settings_update_sends_service_tier() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("thread/settings/update", {})
    client.settings_update(service_tier="priority")
    params = transport.sent_of("thread/settings/update")[0]["params"]
    assert params == {"threadId": "thread-1", "serviceTier": "priority"}


def test_start_turn_raises_when_result_lacks_a_turn_id() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.respond_result("turn/start", {"turn": {"status": "inProgress"}})
    with pytest.raises(CodexAppServerError):
        client.submit("hello", "cid-1")


def test_steer_raises_when_result_lacks_a_turn_id() -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    client.poll_notifications()
    transport.respond_result("turn/steer", {})
    with pytest.raises(CodexAppServerError):
        client.submit("hello", "cid-1")


# =============================================================================
# real WebSocket transport (against a fake connection)
# =============================================================================


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.frames: deque[Any] = deque()
        self.is_closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None) -> Any:
        if not self.frames:
            raise TimeoutError("no frame")
        return self.frames.popleft()

    def close(self) -> None:
        self.is_closed = True


class _ClosedConnection:
    def send(self, message: str) -> None:
        raise ConnectionClosed(None, None)

    def recv(self, timeout: float | None) -> Any:
        raise ConnectionClosed(None, None)


def test_websocket_transport_send_receive_and_close() -> None:
    connection = _FakeConnection()
    transport = WebsocketAppServerTransport(connection=connection)
    transport.send("hello")
    assert connection.sent == ["hello"]
    connection.frames.append("a-string-frame")
    assert transport.receive(1.0) == "a-string-frame"
    connection.frames.append(b"a-bytes-frame")
    assert transport.receive(1.0) == "a-bytes-frame"
    transport.close()
    assert connection.is_closed is True


def test_websocket_transport_maps_connection_closed_to_transport_error() -> None:
    """Both directions surface a dead connection as the typed transport error: a request
    against a closed socket must fail as ``TransportClosedError`` whether the write or the
    read hits the closure first (callers map that error to a retryable not-ready)."""
    transport = WebsocketAppServerTransport(connection=_ClosedConnection())
    with pytest.raises(TransportClosedError):
        transport.receive(1.0)
    with pytest.raises(TransportClosedError):
        transport.send("hello")
