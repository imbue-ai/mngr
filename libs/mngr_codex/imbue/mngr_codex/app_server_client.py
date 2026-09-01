"""A per-agent WebSocket JSON-RPC client for the stock ``codex app-server``.

``mngr_codex`` drives a codex agent by talking to the stock ``codex app-server``
daemon over its WebSocket JSON-RPC surface (``codex app-server --listen
unix://<sock>``), instead of screen-scraping a TUI. This module is that client.

Design (the injected transport)
-------------------------------
All protocol logic lives in :class:`CodexAppServerClient`, which speaks to an
injected transport -- a minimal synchronous ``send`` / ``receive`` / ``close`` duplex
of JSON-RPC text frames. The real transport (:class:`WebsocketAppServerTransport`,
built by :func:`connect_app_server_transport`) wraps a synchronous ``websockets``
connection to the daemon's unix socket; unit tests inject a scripted in-memory
transport instead, so every request/response/notification path is deterministically
testable with no live daemon.

The client is single-threaded and blocking: :meth:`_request` sends a request and then
reads frames until the matching response arrives, dispatching any notifications it
passes along the way (which is also how the tracked active-turn id -- updated from
``turn/started`` / ``turn/completed`` -- stays current, so :meth:`submit` can choose
``turn/start`` (idle) vs ``turn/steer`` (busy)). Callers that want to observe
notifications between requests call :meth:`poll_notifications`.

Transport note (load-bearing): the daemon's WebSocket server rejects the
``permessage-deflate`` extension outright (it closes the connection during the HTTP
upgrade), so the real transport MUST connect with compression disabled.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from collections.abc import Sequence
from enum import Enum
from enum import auto
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final
from typing import Protocol
from typing import runtime_checkable

from loguru import logger
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import PluginMngrError

# The daemon speaks WebSocket over its unix socket (HTTP 101 upgrade). The URI is
# nominal -- the connection target is the socket path -- but a value is required.
_WEBSOCKET_URI: Final[str] = "ws://localhost/"

# JSON-RPC error code the daemon returns for a ``turn/steer`` whose ``expectedTurnId``
# no longer names the active turn (it ended between our decide and our steer -- the
# ABA race). Verified live against codex 0.147.0 ("no active turn to steer").
_NO_ACTIVE_TURN_ERROR_CODE: Final[int] = -32600

# Default bound on a single request's round trip. The daemon answers in single-digit
# milliseconds; a hang means the connection is wedged, so fail rather than block a send.
_DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

# Terminal ``turn.status`` values -- the turn is over and no longer steerable.
_TERMINAL_TURN_STATUSES: Final[frozenset[str]] = frozenset({"completed", "interrupted", "failed"})

# Substring the daemon puts in the ``thread/read`` error when a thread has been started but
# has no first user message yet ("... is not materialized yet; includeTurns is unavailable
# before first user message" -- verified live against codex 0.147.0). For such a thread the
# turns are trivially empty, so a status read retries without ``includeTurns``.
_UNMATERIALIZED_THREAD_MARKER: Final[str] = "not materialized"


class CodexAppServerError(PluginMngrError):
    """A JSON-RPC error from the daemon, or a transport/protocol failure driving it.

    ``code`` mirrors the JSON-RPC error ``code`` when the failure came back as a
    JSON-RPC error object (``None`` for transport/protocol failures with no code).
    """

    def __init__(self, message: str, code: int | None = None, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class TransportClosedError(CodexAppServerError):
    """Raised by a transport's ``send`` or ``receive`` when the underlying connection has closed."""


@runtime_checkable
class AppServerTransport(Protocol):
    """A duplex channel of JSON-RPC text frames to a single app-server connection.

    The real implementation wraps a WebSocket; tests inject a scripted in-memory
    double. ``receive`` returns the next inbound frame and raises ``TimeoutError`` if
    none arrives within ``timeout`` seconds. Both ``send`` and ``receive`` raise
    :class:`TransportClosedError` once the connection is closed, so a request against
    a dead connection surfaces as that typed error on whichever side hits it first.
    """

    def send(self, message: str) -> None: ...

    def receive(self, timeout: float | None) -> str: ...

    def close(self) -> None: ...


class WebsocketAppServerTransport(MutableModel):
    """An :class:`AppServerTransport` backed by a synchronous ``websockets`` connection."""

    model_config = ConfigDict(frozen=False, extra="forbid", arbitrary_types_allowed=True)

    connection: Any

    def send(self, message: str) -> None:
        try:
            self.connection.send(message)
        except ConnectionClosed as exc:
            raise TransportClosedError("app-server websocket connection closed") from exc

    def receive(self, timeout: float | None) -> str:
        try:
            frame = self.connection.recv(timeout)
        except ConnectionClosed as exc:
            raise TransportClosedError("app-server websocket connection closed") from exc
        return frame if isinstance(frame, str) else frame.decode("utf-8")

    def close(self) -> None:
        self.connection.close()


def connect_app_server_transport(socket_path: Path) -> WebsocketAppServerTransport:
    """Open a WebSocket transport to the daemon listening on ``socket_path``.

    Compression is disabled deliberately: the daemon rejects the
    ``permessage-deflate`` extension during the HTTP upgrade and closes the
    connection, so a compressed handshake never completes.
    """
    connection = unix_connect(str(socket_path), uri=_WEBSOCKET_URI, compression=None)
    return WebsocketAppServerTransport(connection=connection)


class DispositionKind(Enum):
    """Whether :meth:`CodexAppServerClient.submit` opened a turn or parked into one."""

    STARTED = "started"
    STEERED = "steered"


class _Unset(Enum):
    """Sentinel distinguishing "omit this field" from an explicit ``None`` (which clears)."""

    UNSET = auto()


UNSET: Final[_Unset] = _Unset.UNSET


class InitializeResult(BaseModel):
    """The daemon's ``initialize`` result."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    user_agent: str = Field(alias="userAgent")
    codex_home: str = Field(alias="codexHome")
    platform_family: str = Field(alias="platformFamily")
    platform_os: str = Field(alias="platformOs")


class ThreadInfo(BaseModel):
    """The seed a ``thread/start`` or ``thread/resume`` response carries.

    ``model`` / ``effort`` / ``service_tier`` are the launch settings the model bar
    seeds from; ``status`` is the raw ``ThreadStatus`` object (e.g. ``{"type": "idle"}``).
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str
    model: str | None = None
    effort: str | None = None
    service_tier: str | None = None
    status: Mapping[str, Any] | None = None

    @classmethod
    def from_response(cls, result: Mapping[str, Any]) -> "ThreadInfo":
        thread = result.get("thread")
        if not isinstance(thread, Mapping) or "id" not in thread:
            raise CodexAppServerError(f"thread response missing a thread id: {result!r}")
        return cls(
            thread_id=str(thread["id"]),
            model=result.get("model"),
            effort=result.get("reasoningEffort"),
            service_tier=result.get("serviceTier"),
            status=thread.get("status"),
        )


class ThreadStatusSnapshot(BaseModel):
    """A point-in-time read of a thread's live status (from ``thread/read``).

    ``status_type`` is the ``ThreadStatus`` tag (``idle`` / ``active`` /
    ``notLoaded`` / ``systemError``); ``active_flags`` are the ``activeFlags`` an
    ``active`` thread carries (``waitingOnApproval`` / ``waitingOnUserInput``);
    ``active_turn_id`` is the id of the in-progress turn, or ``None`` when idle.
    """

    model_config = ConfigDict(frozen=True)

    status_type: str
    active_flags: tuple[str, ...] = ()
    active_turn_id: str | None = None

    @property
    def is_blocked_on_input(self) -> bool:
        """Whether the thread is active AND parked awaiting an approval/input the agent cannot self-clear."""
        return self.status_type == "active" and bool(
            set(self.active_flags) & {"waitingOnApproval", "waitingOnUserInput"}
        )


class ReasoningEffortOption(BaseModel):
    """One selectable reasoning effort for a model, with its human description."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    reasoning_effort: str = Field(alias="reasoningEffort")
    description: str = ""


class ModelServiceTier(BaseModel):
    """One service tier a model offers (the "fast" toggle maps onto these)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    id: str
    name: str = ""
    description: str = ""


class CodexModel(BaseModel):
    """One entry from ``model/list.data`` -- an account-real model and its options."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    id: str
    model: str
    display_name: str = Field(alias="displayName")
    description: str = ""
    hidden: bool = False
    is_default: bool = Field(default=False, alias="isDefault")
    default_reasoning_effort: str | None = Field(default=None, alias="defaultReasoningEffort")
    supported_reasoning_efforts: tuple[ReasoningEffortOption, ...] = Field(
        default=(), alias="supportedReasoningEfforts"
    )
    service_tiers: tuple[ModelServiceTier, ...] = Field(default=(), alias="serviceTiers")
    default_service_tier: str | None = Field(default=None, alias="defaultServiceTier")


class Disposition(BaseModel):
    """What :meth:`CodexAppServerClient.submit` did with a message.

    ``STARTED`` -- opened a fresh turn (the thread was idle); ``STEERED`` -- parked
    into the running turn (the thread was busy). ``turn_id`` is the affected turn.
    Delivery is NOT implied: it is decided later by the committed ``userMessage`` item
    carrying our ``client_id`` (contract A4).
    """

    model_config = ConfigDict(frozen=True)

    kind: DispositionKind
    turn_id: str


# A notification handler receives ``(method, params)`` for every non-response frame.
NotificationHandler = Callable[[str, Mapping[str, Any]], None]


class CodexAppServerClient(MutableModel):
    """Drives one codex ``app-server`` thread over an injected transport.

    Lifecycle: construct with a transport, ``initialize(...)`` to handshake, then
    ``thread_start(...)`` / ``thread_resume(...)`` to bind a thread. Thereafter
    :meth:`submit`, :meth:`interrupt`, :meth:`model_list`, :meth:`settings_update`, and
    :meth:`thread_read` operate on that thread. It is single-threaded and blocking, so
    tests inject a scripted transport and drive it deterministically.
    """

    model_config = ConfigDict(frozen=False, extra="forbid", arbitrary_types_allowed=True)

    transport: Any
    thread_id: str | None = None
    active_turn_id: str | None = None
    # The :class:`ThreadInfo` seed from the most recent ``thread/start`` or ``thread/resume`` on this
    # connection (its ``model`` / ``effort`` / ``service_tier`` launch settings). Captured so a caller
    # that binds through the opener (e.g. system_interface's live connection) can seed the model bar
    # to the settings the daemon resumed with, without a second RPC. ``None`` until a start/resume.
    last_thread_info: "ThreadInfo | None" = None
    next_request_id: int = 1
    notification_handlers: list[NotificationHandler] = Field(default_factory=list)

    # Serializes every frame-level touch of the transport. The client is otherwise a
    # single-threaded blocking driver, but the system_interface persistent connection runs a
    # background reader (``poll_notifications``) alongside request-issuing threads (send /
    # interrupt / settings). Two threads reading frames would each steal the other's frames, so
    # ``_request``, ``_notify``, and ``poll_notifications`` each take this lock for their whole
    # send+read cycle -- they alternate at frame granularity, every frame dispatched exactly once.
    # REENTRANT deliberately: a dispatched notification handler may call back into ``_request`` on
    # the SAME thread (the ledger's reconcile fires ``thread/read`` from inside its notification
    # callback), which a plain lock would self-deadlock; an RLock re-acquires on that thread while
    # still excluding every other one.
    _frame_lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying transport."""
        self.transport.close()

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        """Register a callback invoked with ``(method, params)`` for each notification.

        Handlers see every non-response frame (``turn/*``, ``item/*``, ``thread/*``,
        deltas) the client dispatches. The ledger (system_interface) subscribes here to
        enforce the message-lifecycle contract.
        """
        self.notification_handlers.append(handler)

    def poll_notifications(self, timeout: float | None = 0.0) -> None:
        """Read and dispatch any frames already available (up to ``timeout`` seconds each).

        Used by a caller that wants to observe notifications while no request is in
        flight. Stops once no frame arrives within ``timeout``; a malformed frame is
        logged and skipped rather than treated as the end of the stream.

        Holds :attr:`_frame_lock` for one drain of the already-buffered burst so a concurrent
        request's send+read cycle never interleaves with it, then releases so a waiting sender
        (e.g. a fire-and-forget interrupt) is not pinned behind a long stream. Only the FIRST
        frame waits up to ``timeout``; subsequent frames are drained non-blocking.
        """
        with self._frame_lock:
            raw = self._next_raw_frame(timeout)
            while raw is not None:
                message = self._parse_frame(raw)
                if message is not None:
                    self._dispatch_frame(message)
                # Drain subsequent frames NON-BLOCKING (timeout 0): only what is already buffered.
                # A heavily-streaming turn otherwise keeps this loop fed within ``timeout`` per frame
                # and pins ``_frame_lock`` for seconds, delaying a concurrent interrupt/steer past the
                # "immediate" bar (contract A5). Draining only the buffered burst then releasing lets a
                # waiting sender interleave; the reader re-acquires on its next poll.
                raw = self._next_raw_frame(0.0)

    # -- request/notification plumbing ------------------------------------

    def _next_request_id(self) -> int:
        request_id = self.next_request_id
        self.next_request_id = request_id + 1
        return request_id

    def _next_raw_frame(self, timeout: float | None) -> str | None:
        """Return the next raw inbound frame, or ``None`` if none arrived within ``timeout``."""
        try:
            return self.transport.receive(timeout)
        except TimeoutError:
            return None

    def _parse_frame(self, raw: str) -> Mapping[str, Any] | None:
        """Parse a raw frame into a JSON object, or ``None`` if it is malformed."""
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring non-JSON frame from codex app-server: {}", raw)
            return None
        if not isinstance(message, Mapping):
            logger.warning("Ignoring non-object frame from codex app-server: {}", raw)
            return None
        return message

    def _dispatch_frame(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        typed_params = params if isinstance(params, Mapping) else {}
        self._track_turn_state(method, typed_params)
        for handler in self.notification_handlers:
            handler(method, typed_params)

    def _track_turn_state(self, method: str, params: Mapping[str, Any]) -> None:
        """Keep ``active_turn_id`` current from the turn lifecycle notifications."""
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            return
        turn_id = turn.get("id")
        if method == "turn/started":
            if isinstance(turn_id, str):
                self.active_turn_id = turn_id
        elif method == "turn/completed":
            if turn.get("status") in _TERMINAL_TURN_STATUSES and turn_id == self.active_turn_id:
                self.active_turn_id = None
        else:
            return

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        """Issue a JSON-RPC request and read frames until its result (raising on error).

        Holds :attr:`_frame_lock` across the whole send-then-read-until-response cycle so a
        background ``poll_notifications`` reader cannot consume this request's response (or its
        interleaved notifications) out from under it.
        """
        with self._frame_lock:
            request_id = self._next_request_id()
            payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
            self.transport.send(payload)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                raw = self._next_raw_frame(max(0.0, deadline - time.monotonic()))
                if raw is None:
                    continue
                frame = self._parse_frame(raw)
                if frame is None:
                    continue
                if frame.get("id") == request_id and ("result" in frame or "error" in frame):
                    return self._result_or_raise(frame, method)
                self._dispatch_frame(frame)
        raise CodexAppServerError(f"codex app-server request {method!r} timed out")

    def _result_or_raise(self, frame: Mapping[str, Any], method: str) -> Mapping[str, Any]:
        error = frame.get("error")
        if error is not None:
            raise CodexAppServerError(
                f"codex app-server request {method!r} failed: {error.get('message', error)!r}",
                code=error.get("code"),
                data=error.get("data"),
            )
        result = frame.get("result")
        return result if isinstance(result, Mapping) else {}

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": dict(params)})
        with self._frame_lock:
            self.transport.send(payload)

    # -- protocol methods --------------------------------------------------

    def initialize(self, client_name: str, client_version: str) -> InitializeResult:
        """Handshake: ``initialize`` (experimentalApi) then the ``initialized`` notification.

        ``experimentalApi`` is required for the model/settings surface; without it
        ``thread/settings/update`` fails ``-32600 "requires experimentalApi capability"``.
        """
        result = self._request(
            "initialize",
            {
                "clientInfo": {"name": client_name, "version": client_version},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._notify("initialized", {})
        return InitializeResult.model_validate(result)

    def thread_start(
        self,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> ThreadInfo:
        """Start a fresh thread and bind it; the response carries the model-bar seed."""
        params = _drop_none({"cwd": cwd, "model": model, "effort": effort, "serviceTier": service_tier})
        info = ThreadInfo.from_response(self._request("thread/start", params))
        self.thread_id = info.thread_id
        self.last_thread_info = info
        return info

    def thread_resume(
        self,
        thread_id: str,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> ThreadInfo:
        """Resume ``thread_id`` from its on-disk rollout and bind it.

        Raises :class:`CodexAppServerError` (``-32600``) if no rollout exists yet.
        """
        params = _drop_none(
            {"threadId": thread_id, "cwd": cwd, "model": model, "effort": effort, "serviceTier": service_tier}
        )
        info = ThreadInfo.from_response(self._request("thread/resume", params))
        self.thread_id = info.thread_id
        self.last_thread_info = info
        return info

    def inject_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        """Inject ``items`` into the bound thread WITHOUT running a model turn.

        This is how mngr *materializes* a freshly ``thread_start``-ed thread: ``thread/start``
        alone leaves the thread unmaterialized (no rollout on disk, so ``codex resume <id>`` /
        a daemon cold-load cannot find it -- verified live, ``-32600 no rollout found``), while
        ``thread/inject_items`` writes the rollout with no model call. The daemon rejects an
        empty ``items`` list (``items must not be empty``), so the caller passes at least one
        item; a single ``environmentContext`` item materializes the session the way codex seeds
        one itself, without adding a visible user-message bubble (verified live against codex
        0.147: the rollout gets ``session_meta`` / ``response_item`` / ``world_state`` /
        ``turn_context`` records, no user turn).
        """
        thread_id = self._require_thread_id()
        self._request("thread/inject_items", {"threadId": thread_id, "items": list(items)})

    def thread_read(self, include_turns: bool = False) -> Mapping[str, Any]:
        """Read the bound thread (optionally with its turns) -- the reconcile backstop."""
        thread_id = self._require_thread_id()
        return self._request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})

    def thread_loaded_list(self) -> tuple[str, ...]:
        """Return the ids of the threads the daemon currently holds loaded in memory.

        ``thread/loaded/list`` is how a FRESH connection discovers the thread the
        daemon is driving without any persisted id: a thread stays loaded (and any
        running turn keeps going) after the connection that started it closes, so a
        later short-lived send finds it here. The set may also include sub-agent
        threads, so callers disambiguate the root via a persisted id.
        """
        result = self._request("thread/loaded/list", {})
        data = result.get("data")
        if not isinstance(data, list):
            return ()
        return tuple(entry for entry in data if isinstance(entry, str))

    def bind_thread(self, thread_id: str) -> None:
        """Bind an already-loaded thread by id, without an RPC.

        Used for a thread discovered via :meth:`thread_loaded_list`: the daemon
        already holds it loaded, so ``thread/read`` / ``turn/*`` work against it
        directly (verified live) with no ``thread/start`` or ``thread/resume``.
        """
        self.thread_id = thread_id

    def read_thread_status(self) -> ThreadStatusSnapshot:
        """Read the bound thread's LIVE status and re-seed ``active_turn_id`` from it.

        A short-lived connection has no notification history, so it cannot know from
        ``turn/*`` events whether a turn is running. This queries the daemon's live
        state (``thread/read{includeTurns:true}``) and sets ``active_turn_id`` to the
        in-progress turn (or ``None`` when idle), so the very next :meth:`submit`
        chooses ``turn/steer`` (busy -> park) vs ``turn/start`` (idle -> deliver)
        correctly -- a blind ``turn/start`` while busy would open a SECOND concurrent
        turn (verified live).

        A thread that was started but never messaged is "not materialized": the daemon
        rejects ``includeTurns:true`` for it (verified live). Such a thread has no turns,
        so this retries with ``includeTurns:false`` and reads status only (``active_turn_id``
        is then ``None``, which is correct -- nothing is running yet).
        """
        result = self._read_thread_tolerating_unmaterialized()
        thread = result.get("thread")
        thread_map: Mapping[str, Any] = thread if isinstance(thread, Mapping) else {}
        status = thread_map.get("status")
        status_map: Mapping[str, Any] = status if isinstance(status, Mapping) else {}
        status_type = str(status_map.get("type", "notLoaded"))
        raw_flags = status_map.get("activeFlags")
        active_flags = (
            tuple(flag for flag in raw_flags if isinstance(flag, str)) if isinstance(raw_flags, list) else ()
        )
        active_turn_id = _find_in_progress_turn_id(thread_map.get("turns"))
        self.active_turn_id = active_turn_id
        return ThreadStatusSnapshot(status_type=status_type, active_flags=active_flags, active_turn_id=active_turn_id)

    def _read_thread_tolerating_unmaterialized(self) -> Mapping[str, Any]:
        """``thread/read{includeTurns:true}``, retried without turns for an unmaterialized thread."""
        try:
            return self.thread_read(include_turns=True)
        except CodexAppServerError as exc:
            if _UNMATERIALIZED_THREAD_MARKER not in str(exc):
                raise
            return self.thread_read(include_turns=False)

    def model_list(self, include_hidden: bool = False) -> tuple[CodexModel, ...]:
        """Return the account's models from ``model/list`` (envelope key ``data``)."""
        result = self._request("model/list", {"includeHidden": include_hidden})
        data = result.get("data")
        if not isinstance(data, list):
            raise CodexAppServerError(f"model/list result missing a 'data' array: {result!r}")
        return tuple(CodexModel.model_validate(entry) for entry in data)

    def settings_update(
        self,
        model: str | None | _Unset = UNSET,
        effort: str | None | _Unset = UNSET,
        service_tier: str | None | _Unset = UNSET,
    ) -> None:
        """Update thread settings (``null`` clears, an omitted arg leaves unchanged).

        The daemon does not hard-fail an unavailable model here; callers detect a failed
        switch by comparing the emitted ``thread/settings/updated`` value.
        """
        thread_id = self._require_thread_id()
        params: dict[str, Any] = {"threadId": thread_id}
        if not isinstance(model, _Unset):
            params["model"] = model
        if not isinstance(effort, _Unset):
            params["effort"] = effort
        if not isinstance(service_tier, _Unset):
            params["serviceTier"] = service_tier
        self._request("thread/settings/update", params)

    def interrupt(self, turn_id: str) -> None:
        """Interrupt ``turn_id`` on the bound thread (reserved for Stop)."""
        thread_id = self._require_thread_id()
        self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def interrupt_nowait(self, turn_id: str) -> None:
        """Send ``turn/interrupt`` fire-and-forget: write the request, do NOT wait for its response.

        The turn actually aborting is a multi-second operation (a mid-stream interrupt can take
        several seconds), and blocking on it delays the caller's own response (contract A5). This
        writes the ``turn/interrupt`` request frame under the frame lock -- so it is ordered against
        the reader and any concurrent request -- and returns immediately, WITHOUT reading the
        matching response. That id-matched response frame is later read and harmlessly dropped by the
        background reader (or the next request's read loop), since a response carries no ``method``
        and so dispatches to no handler. The authoritative settle arrives out-of-band via the
        subscribed ``turn/completed(interrupted)`` notification.
        """
        thread_id = self._require_thread_id()
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "turn/interrupt",
                "params": {"threadId": thread_id, "turnId": turn_id},
            }
        )
        with self._frame_lock:
            self.transport.send(payload)

    def submit(self, text: str, client_id: str) -> Disposition:
        """Deliver ``text`` as a user turn if idle, else park it into the running turn.

        Idle -> ``turn/start`` (Disposition ``STARTED``); busy -> ``turn/steer`` against
        the tracked active turn (Disposition ``STEERED``). ``client_id`` is threaded as
        ``clientUserMessageId`` so the committed ``userMessage`` echoes it back for
        delivery reconciliation. On the ABA race (``-32600`` -- the turn ended between
        our decide and our steer) it re-decides exactly once against the freshly-tracked
        turn state.
        """
        self._require_thread_id()
        return self._submit_once(text, client_id, is_retry_allowed=True)

    def _submit_once(self, text: str, client_id: str, is_retry_allowed: bool) -> Disposition:
        thread_id = self._require_thread_id()
        active_turn_id = self.active_turn_id
        text_input = [{"type": "text", "text": text}]
        if active_turn_id is None:
            return self._start_turn(thread_id, text_input, client_id)
        return self._steer_turn(thread_id, active_turn_id, text_input, client_id, is_retry_allowed, text)

    def _start_turn(self, thread_id: str, text_input: Sequence[Mapping[str, Any]], client_id: str) -> Disposition:
        result = self._request(
            "turn/start",
            {"threadId": thread_id, "input": text_input, "clientUserMessageId": client_id},
        )
        turn = result.get("turn")
        if not isinstance(turn, Mapping) or "id" not in turn:
            raise CodexAppServerError(f"turn/start result missing a turn id: {result!r}")
        new_turn_id = str(turn["id"])
        self.active_turn_id = new_turn_id
        return Disposition(kind=DispositionKind.STARTED, turn_id=new_turn_id)

    def _steer_turn(
        self,
        thread_id: str,
        expected_turn_id: str,
        text_input: Sequence[Mapping[str, Any]],
        client_id: str,
        is_retry_allowed: bool,
        text: str,
    ) -> Disposition:
        try:
            result = self._request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": expected_turn_id,
                    "input": text_input,
                    "clientUserMessageId": client_id,
                },
            )
        except CodexAppServerError as exc:
            if not (is_retry_allowed and exc.code == _NO_ACTIVE_TURN_ERROR_CODE):
                raise
            # ABA: the expected turn ended between our decide and our steer. If the last
            # frame we processed has not moved past it, clear it so the retry re-decides
            # as a fresh start; if a successor turn is already tracked, the retry steers it.
            if self.active_turn_id == expected_turn_id:
                self.active_turn_id = None
            return self._submit_once(text, client_id, is_retry_allowed=False)
        steered_turn_id = result.get("turnId")
        if not isinstance(steered_turn_id, str):
            raise CodexAppServerError(f"turn/steer result missing a turnId: {result!r}")
        return Disposition(kind=DispositionKind.STEERED, turn_id=steered_turn_id)

    def _require_thread_id(self) -> str:
        if self.thread_id is None:
            raise CodexAppServerError("no thread is bound; call thread_start or thread_resume first")
        return self.thread_id


def _drop_none(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``params`` without keys whose value is ``None`` (an omitted optional)."""
    return {key: value for key, value in params.items() if value is not None}


def _find_in_progress_turn_id(turns: Any) -> str | None:
    """Return the id of the in-progress turn among ``thread.turns``, or ``None``.

    A thread has at most one running turn; if several are somehow reported the last
    one wins (it is the most recent). A malformed ``turns`` value yields ``None``.
    """
    if not isinstance(turns, list):
        return None
    in_progress_id: str | None = None
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        if turn.get("status") == "inProgress":
            turn_id = turn.get("id")
            if isinstance(turn_id, str):
                in_progress_id = turn_id
    return in_progress_id
