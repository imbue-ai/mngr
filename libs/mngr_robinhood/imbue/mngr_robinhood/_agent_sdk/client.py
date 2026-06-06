"""mngr-backed ``query()`` and ``ClaudeSDKClient``.

These mirror the documented ``claude_agent_sdk`` async surface but drive a ``robinhood-``
prefixed mngr claude agent instead of a directly-spawned ``claude`` subprocess. The async
methods bridge to mngr's synchronous in-process API by running the blocking turn-driver in a
worker thread via ``asyncio.to_thread`` (the SDK surface is async; the mngr API and the
transcript-polling turn-driver are synchronous).

Status: the lifecycle/streaming/multi-turn structure here is complete; the single private
hook that actually creates the agent and drives a turn (``_drain_turn_messages``) is the live-wiring seam
that is built out and verified against a real claude agent in the next phase. It currently
raises :class:`AgentSdkNotImplementedError` with the precise intended wiring documented inline.
"""

from collections.abc import AsyncIterator
from collections.abc import Mapping
from typing import Any
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
from imbue.mngr_robinhood._agent_sdk.driver import LiveSession
from imbue.mngr_robinhood._agent_sdk.driver import deliver_turn
from imbue.mngr_robinhood._agent_sdk.driver import drain_turn
from imbue.mngr_robinhood._agent_sdk.driver import start_session
from imbue.mngr_robinhood._agent_sdk.driver import stop_session
from imbue.mngr_robinhood.errors import RobinhoodError


class AgentSdkNotImplementedError(RobinhoodError, NotImplementedError):
    """Raised by mngr-backed SDK surfaces that are designed but not yet wired to a live agent."""


# Prompt accepted by ``query`` / ``ClaudeSDKClient.query``: either a single string turn or the
# documented streaming-input form (an async iterable of user-message dicts).
PromptInput = str | AsyncIterator[dict[str, Any]]


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
        self.is_connected = True
        if prompt is not None:
            await self.query(prompt)

    async def disconnect(self) -> None:
        """Stop the underlying mngr claude agent, leaving its session readable for later."""
        if not self.is_connected:
            return
        if self.session is not None:
            await to_thread.run_sync(stop_session, self.session)
        self.is_connected = False

    async def query(self, prompt: PromptInput, session_id: str | None = None) -> None:
        """Deliver one user turn to the connected agent (string or streaming-input form)."""
        if not self.is_connected or self.session is None:
            raise RobinhoodError("ClaudeSDKClient.query called before connect()")
        prompt_text = await _coerce_prompt_to_text(prompt)
        await to_thread.run_sync(deliver_turn, self.session, prompt_text)

    async def receive_messages(self) -> AsyncIterator[Message]:
        """Low-level stream of all messages for the current turn (does not stop on its own)."""
        if self.session is None:
            raise RobinhoodError("ClaudeSDKClient.receive_messages called before connect()")
        for message in await to_thread.run_sync(drain_turn, self.session):
            yield message

    async def receive_response(self) -> AsyncIterator[Message]:
        """Stream the current turn's messages, terminating at (and including) the ResultMessage."""
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return

    async def set_model(self, model: str | None = None) -> None:
        """Accept a mid-session model change request and keep the session usable.

        mngr cannot swap the model of an already-running claude agent over its transport, so this
        is a no-op against the live agent (it does not break the session); a model change only
        takes effect for a freshly-created agent.
        """
        logger.debug("set_model requested ({}); not applied to the already-running mngr agent", model)

    async def set_permission_mode(self, mode: str) -> None:
        """Accept a mid-session permission-mode change request (see :meth:`set_model` for the caveat)."""
        logger.debug("set_permission_mode requested ({}); not applied to the already-running mngr agent", mode)

    async def interrupt(self) -> None:
        """Interrupt the in-flight turn. Not yet supported by the mngr-backed transport."""
        raise AgentSdkNotImplementedError(
            "ClaudeSDKClient.interrupt is not supported by the mngr-backed transport yet."
        )

    async def get_server_info(self) -> dict[str, Any]:
        """Return server info (commands / output style). Not surfaced by the mngr transport yet."""
        raise AgentSdkNotImplementedError("ClaudeSDKClient.get_server_info is not surfaced by the mngr transport.")

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
