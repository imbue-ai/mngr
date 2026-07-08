"""mngr-backed ``query()`` and ``ClaudeSDKClient``.

These mirror the documented ``claude_agent_sdk`` async surface but drive a ``robinhood-``
prefixed mngr claude agent (via :mod:`._agent_sdk.driver`) instead of a directly-spawned
``claude`` subprocess. The async methods bridge to mngr's synchronous in-process API by running
the blocking turn-driver in a worker thread via ``anyio.to_thread`` (the SDK surface is async;
the mngr API and the transcript-polling turn-driver are synchronous).

Control-surface methods are mapped onto mngr mechanisms: ``interrupt`` stops the agent mid-turn,
``set_model`` / ``set_permission_mode`` restart it on the resumed session under new options, and
``get_server_info`` runs a one-shot ``claude`` probe. Partial-message ``StreamEvent`` streaming is
the one documented surface the session-JSONL transport cannot provide.
"""

import queue
from collections.abc import AsyncIterator
from collections.abc import Mapping
from threading import Thread
from typing import Any
from typing import Final
from typing import Self

from anyio import to_thread
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import Message
from claude_agent_sdk import ResultMessage
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr_robinhood._agent_sdk.driver import LiveSession
from imbue.mngr_robinhood._agent_sdk.driver import deliver_turn
from imbue.mngr_robinhood._agent_sdk.driver import drain_turn
from imbue.mngr_robinhood._agent_sdk.driver import interrupt_session
from imbue.mngr_robinhood._agent_sdk.driver import reconfigure_session
from imbue.mngr_robinhood._agent_sdk.driver import start_session
from imbue.mngr_robinhood._agent_sdk.driver import stop_session
from imbue.mngr_robinhood._agent_sdk.hook_bridge import is_hook_bridge_needed
from imbue.mngr_robinhood._agent_sdk.hook_bridge import start_hook_bridge
from imbue.mngr_robinhood._agent_sdk.server_info import probe_server_info
from imbue.mngr_robinhood.errors import RobinhoodError

# Sentinel pushed onto the message stream by the drain worker to signal end-of-turn.
_DRAIN_SENTINEL: Final[object] = object()

# Prompt accepted by ``query`` / ``ClaudeSDKClient.query``: either a single string turn or the
# documented streaming-input form (an async iterable of user-message dicts).
PromptInput = str | AsyncIterator[dict[str, Any]]


def _drain_worker(session: LiveSession, stream: "_TurnMessageStream") -> None:
    """Run the (blocking) turn drain in a background thread, pushing each message onto the queue.

    Any exception raised by the drain is recorded on ``stream`` (and re-raised to the consumer when
    it reaches the sentinel) rather than being swallowed by the thread's default excepthook, so a
    drain failure surfaces to the SDK caller instead of looking like a clean end-of-turn.
    """
    try:
        drain_turn(session, stream.message_queue.put)
    except MngrError as exc:
        stream.drain_error = exc
    finally:
        stream.message_queue.put(_DRAIN_SENTINEL)


class _TurnMessageStream(MutableModel):
    """Async iterator over one turn's messages, fed by a background drain thread via a queue.

    The drain runs in a plain thread (not a task group) so this stream can be iterated from -- and
    closed by -- an async generator without the cross-task cancel-scope errors that a task group
    spanning a ``yield`` would cause. ``__anext__`` blocks a worker thread on ``queue.get`` so the
    event loop stays free for ``interrupt()`` to land between messages.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    message_queue: SkipValidation["queue.Queue[Any]"] = Field(description="Queue the drain thread pushes onto")
    worker_thread: SkipValidation[Thread | None] = Field(
        default=None, description="Background thread running the turn drain (set once started)"
    )
    drain_error: SkipValidation["MngrError | None"] = Field(
        default=None, description="Exception raised by the drain thread, re-raised to the consumer at end-of-turn"
    )

    def __aiter__(self) -> "_TurnMessageStream":
        return self

    async def __anext__(self) -> Message:
        item = await to_thread.run_sync(self.message_queue.get)
        if item is _DRAIN_SENTINEL:
            if self.drain_error is not None:
                raise self.drain_error
            raise StopAsyncIteration
        return item


def _start_turn_message_stream(session: LiveSession) -> _TurnMessageStream:
    """Start the background drain thread for one turn and return its async-iterable message stream."""
    message_queue: queue.Queue[Any] = queue.Queue()
    stream = _TurnMessageStream(message_queue=message_queue)
    worker_thread = Thread(target=_drain_worker, args=(session, stream), name="mngr-sdk-turn-drain", daemon=True)
    stream.worker_thread = worker_thread
    worker_thread.start()
    return stream


async def query(
    *,
    prompt: PromptInput,
    options: ClaudeAgentOptions | None = None,
) -> AsyncIterator[Message]:
    """Run a single one-shot turn and async-yield its messages, ending in a ``ResultMessage``.

    Mirrors ``claude_agent_sdk.query``: spins up a fresh ``robinhood-`` mngr claude agent in the
    options' ``cwd``, delivers ``prompt``, streams the parsed transcript messages
    (``SystemMessage`` init -> ``AssistantMessage``/``UserMessage`` -> terminal
    ``ResultMessage``), then stops the agent (leaving its session readable).
    """
    client = ClaudeSDKClient() if options is None else ClaudeSDKClient(options=options)
    await client.connect(prompt)
    try:
        async for message in client.receive_response():
            yield message
    finally:
        await client.disconnect()


class ClaudeSDKClient(MutableModel):
    """mngr-backed implementation of the documented ``ClaudeSDKClient`` lifecycle + control surface.

    One client owns one mngr claude agent (one session) for its connected lifetime. Turns are
    delivered with ``query`` and read with ``receive_response`` / ``receive_messages``; the
    agent stays alive across turns and is stopped (not destroyed) on ``disconnect``.
    """

    # The live mngr handles attached after ``connect`` are external (non-pydantic) types.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ``ClaudeAgentOptions`` is an external (non-pydantic) dataclass whose fields pydantic cannot
    # build a schema for, so it is held with ``SkipValidation``: the static type is preserved
    # while pydantic does not introspect or validate it.
    options: SkipValidation[ClaudeAgentOptions] = Field(
        default_factory=ClaudeAgentOptions, description="Resolved options governing the session"
    )
    is_connected: bool = Field(default=False, description="Whether the underlying agent is live")
    session: LiveSession | None = Field(default=None, description="Live mngr agent state, once connected")

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.disconnect()

    async def connect(self, prompt: PromptInput | None = None) -> None:
        """Establish the session (the agent is created lazily on the first turn); optionally deliver a first turn.

        The blocking mngr context/agent work runs in a worker thread so the event loop is not
        blocked (the documented SDK surface is async; the mngr API is synchronous).
        """
        self.session = await to_thread.run_sync(start_session, self.options)
        self._start_hook_bridge_if_needed()
        self.is_connected = True
        if prompt is None:
            return
        # If the first turn fails the session is still live (the concurrency group is entered and
        # an agent may have been created); disconnect it before propagating so it is not leaked.
        # __aexit__ is not called when __aenter__/connect raises, and the module-level ``query``
        # awaits connect() outside its try/finally, so cleanup has to happen here. A success flag
        # (rather than a broad ``except``) keeps the teardown unconditional without narrowing the
        # propagating exception.
        is_first_turn_delivered = False
        try:
            await self.query(prompt)
            is_first_turn_delivered = True
        finally:
            if not is_first_turn_delivered:
                await self.disconnect()

    def _start_hook_bridge_if_needed(self) -> None:
        """Start the in-process can_use_tool / hooks bridge when the options request callbacks.

        The bridge runs callbacks on its own anyio portal thread (created by start_hook_bridge) and
        records permission denials into the live session (surfaced in ResultMessage.permission_denials).
        """
        session = self.session
        if session is None or not is_hook_bridge_needed(self.options):
            return
        session.hook_bridge = start_hook_bridge(
            self.options, lambda denial: session.turn_permission_denials.append(denial)
        )

    async def disconnect(self) -> None:
        """Stop the underlying mngr claude agent, leaving its session readable for later."""
        if not self.is_connected:
            return
        if self.session is not None:
            # Stop the bridge even if stopping the agent raises, so its HTTP server / portal thread /
            # temp settings dir are never leaked on a teardown failure.
            try:
                await to_thread.run_sync(stop_session, self.session)
            finally:
                if self.session.hook_bridge is not None:
                    await to_thread.run_sync(self.session.hook_bridge.stop)
                    self.session.hook_bridge = None
        self.is_connected = False

    async def query(self, prompt: PromptInput, session_id: str | None = None) -> None:
        """Deliver one user turn to the connected agent (string or streaming-input form)."""
        if not self.is_connected or self.session is None:
            raise RobinhoodError("ClaudeSDKClient.query called before connect()")
        prompt_text = await _coerce_prompt_to_text(prompt)
        await to_thread.run_sync(deliver_turn, self.session, prompt_text)

    async def receive_messages(self) -> AsyncIterator[Message]:
        """Stream the current turn's messages as they arrive (init, content, terminal result).

        The synchronous transcript-polling drain runs in a worker thread and pushes each parsed
        message onto an anyio memory stream; this coroutine yields them as they appear. Streaming
        (rather than batching the whole turn) is what lets ``interrupt()``, called from the consumer
        between yields, stop the in-flight turn -- the drain then observes the agent's death and
        finalizes.
        """
        session = self.session
        if session is None:
            raise RobinhoodError("ClaudeSDKClient.receive_messages called before connect()")
        async for message in _start_turn_message_stream(session):
            yield message

    async def receive_response(self) -> AsyncIterator[Message]:
        """Stream the current turn's messages, terminating at (and including) the ResultMessage."""
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return

    async def set_model(self, model: str | None = None) -> None:
        """Apply a mid-session model change by restarting the agent on the resumed session.

        mngr cannot swap a running claude process's model, so the current session is adopted into a
        fresh agent that resumes it under the new model (the old agent is stopped). The session
        stays usable for the next turn.
        """
        if not self.is_connected or self.session is None:
            raise RobinhoodError("ClaudeSDKClient.set_model called before connect()")
        logger.debug("Applying set_model ({}) by recreating the mngr agent on the resumed session", model)
        await to_thread.run_sync(reconfigure_session, self.session, model, None)

    async def set_permission_mode(self, mode: str) -> None:
        """Apply a mid-session permission-mode change (see :meth:`set_model` for the mechanism)."""
        if not self.is_connected or self.session is None:
            raise RobinhoodError("ClaudeSDKClient.set_permission_mode called before connect()")
        logger.debug("Applying set_permission_mode ({}) by recreating the mngr agent on the resumed session", mode)
        await to_thread.run_sync(reconfigure_session, self.session, None, mode)

    async def interrupt(self) -> None:
        """Interrupt the in-flight turn by stopping the agent; the response stream then ends.

        The agent is left stopped (not destroyed); a subsequent ``query()`` restarts it with
        ``--resume`` so the conversation continues.
        """
        if not self.is_connected or self.session is None:
            raise RobinhoodError("ClaudeSDKClient.interrupt called before connect()")
        await to_thread.run_sync(interrupt_session, self.session)

    async def get_server_info(self) -> dict[str, Any]:
        """Return server info (available slash commands / output style), via a one-shot probe.

        The data comes from claude's ``system``/``init`` event, captured by running a single
        ``claude -p ... --output-format stream-json`` probe in the session's cwd and cached, since
        mngr's session-JSONL transport never sees the control-protocol initialize response.
        """
        if self.session is None:
            raise RobinhoodError("ClaudeSDKClient.get_server_info called before connect()")
        if self.session.server_info is None:
            self.session.server_info = await to_thread.run_sync(
                probe_server_info, self.session.concurrency_group, self.session.cwd, self.options.model
            )
        return dict(self.session.server_info)

    async def get_mcp_status(self) -> Mapping[str, Any]:
        """Return MCP server status. The SDK configures no MCP servers, so the list is empty."""
        return {"mcpServers": []}


async def _coerce_prompt_to_text(prompt: PromptInput) -> str:
    """Collapse a string or streaming-input prompt into the text to deliver to the agent.

    mngr delivers a turn as a single message string, so the documented streaming-input form
    (an async iterable of ``{"type": "user", "message": {"role": "user", "content": ...}}``
    dicts) is flattened into its concatenated user text.
    """
    if isinstance(prompt, str):
        return prompt
    parts: list[str] = []
    async for chunk in prompt:
        parts.append(_extract_user_text(chunk))
    return "\n".join(part for part in parts if part)


def _extract_user_text(chunk: Mapping[str, Any]) -> str:
    """Extract the user text from one streaming-input message dict."""
    message = chunk.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)
    return ""
