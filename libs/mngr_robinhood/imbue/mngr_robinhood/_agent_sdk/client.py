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

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import Message
from claude_agent_sdk import ResultMessage
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.imbue_common.mutable_model import MutableModel
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

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.disconnect()

    async def connect(self, prompt: PromptInput | None = None) -> None:
        """Create the underlying mngr claude agent; optionally deliver an initial prompt."""
        self._connect_agent()
        self.is_connected = True
        if prompt is not None:
            await self.query(prompt)

    async def disconnect(self) -> None:
        """Stop the underlying mngr claude agent, leaving its session readable for later."""
        if not self.is_connected:
            return
        self._stop_agent()
        self.is_connected = False

    async def query(self, prompt: PromptInput, session_id: str | None = None) -> None:
        """Deliver one user turn to the connected agent (string or streaming-input form)."""
        if not self.is_connected:
            raise RobinhoodError("ClaudeSDKClient.query called before connect()")
        prompt_text = await _coerce_prompt_to_text(prompt)
        self._deliver_turn(prompt_text)

    async def receive_messages(self) -> AsyncIterator[Message]:
        """Low-level stream of all messages for the current turn (does not stop on its own)."""
        for message in await self._drain_turn_messages():
            yield message

    async def receive_response(self) -> AsyncIterator[Message]:
        """Stream the current turn's messages, terminating at (and including) the ResultMessage."""
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return

    async def interrupt(self) -> None:
        """Interrupt the in-flight turn. Deferred control surface (next phase)."""
        raise AgentSdkNotImplementedError(
            "ClaudeSDKClient.interrupt is not yet wired; it will deliver an interrupt to the "
            "running mngr agent (analogous to the orchestrator's SIGINT handling)."
        )

    async def set_model(self, model: str | None = None) -> None:
        """Switch the model mid-session. Deferred control surface (next phase)."""
        raise AgentSdkNotImplementedError("ClaudeSDKClient.set_model is not yet wired.")

    async def set_permission_mode(self, mode: str) -> None:
        """Switch the permission mode mid-session. Deferred control surface (next phase)."""
        raise AgentSdkNotImplementedError("ClaudeSDKClient.set_permission_mode is not yet wired.")

    async def get_server_info(self) -> dict[str, Any]:
        """Return server info (commands / output style). Deferred (next phase)."""
        raise AgentSdkNotImplementedError("ClaudeSDKClient.get_server_info is not yet wired.")

    async def get_mcp_status(self) -> Mapping[str, Any]:
        """Return MCP server status. Deferred (next phase)."""
        raise AgentSdkNotImplementedError("ClaudeSDKClient.get_mcp_status is not yet wired.")

    # --- live-wiring seam ---------------------------------------------------------------------
    # The methods below are the only ones that touch the mngr runtime. They are implemented and
    # verified against a real claude agent in the next phase; the async surface above is final.

    def _connect_agent(self) -> None:
        """Create the ``robinhood-`` mngr claude agent for this session.

        Intended wiring (mirrors ``orchestrator._run_with_agent``): build a zero-config
        ``MngrContext`` (see ``_agent_sdk.context``), translate ``self.options`` into claude
        ``agent_args`` (model/permission-mode/allowed-tools/add-dir/system-prompt/
        setting-sources/resume) plus ``AgentEnvironmentOptions`` (``env``), then call
        ``imbue.mngr.api.create.create`` with ``agent_type="claude"`` and a ``robinhood-`` name.
        Hold the agent/host/events-target and a ``RawTranscriptParser`` as instance state.
        """
        raise AgentSdkNotImplementedError(
            "ClaudeSDKClient live agent creation is implemented in the next phase; the option "
            "-> claude-flag mapping and create() call are documented in this method."
        )

    def _deliver_turn(self, prompt_text: str) -> None:
        """Deliver a user turn to the live agent (initial message on create, else send_message)."""
        raise AgentSdkNotImplementedError("ClaudeSDKClient live turn delivery is implemented in the next phase.")

    async def _drain_turn_messages(self) -> list[Message]:
        """Drive the current turn to end and return its parsed SDK messages.

        Intended wiring: in a worker thread (``asyncio.to_thread``), poll the agent's raw
        transcript via the shared turn-driver until the terminal ``stop_reason`` arrives (as
        ``orchestrator._wait_for_turn_end`` does), parse new lines into SDK messages with
        ``_agent_sdk.message_parser``, prepend a synthesized ``SystemMessage`` init on the first
        turn, and append a synthesized terminal ``ResultMessage``.
        """
        raise AgentSdkNotImplementedError("ClaudeSDKClient live turn draining is implemented in the next phase.")

    def _stop_agent(self) -> None:
        """Stop (not destroy) the live agent so its session remains readable."""
        raise AgentSdkNotImplementedError("ClaudeSDKClient live agent stop is implemented in the next phase.")


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
