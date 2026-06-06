"""Shared helpers for the live Claude Agent SDK test suite (test_sdk_*.py).

These factor out the setup, stream-collection, and validation logic that is otherwise copy-pasted
across the SDK test files: building the hermetic ``ClaudeAgentOptions``, running ``query()`` or a
``ClaudeSDKClient`` turn to completion, and extracting the assistant text or terminal
``ResultMessage`` from a message stream. They are non-fixture utilities, so they live here (per the
project's testing conventions) rather than in conftest.py.
"""

from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import TextBlock


def make_sdk_options(model: str, cwd: Path, **overrides: Any) -> ClaudeAgentOptions:
    """Build hermetic ``ClaudeAgentOptions`` for a live SDK test.

    Pins the model and an isolated ``cwd`` and sets ``setting_sources=[]`` so the agent does not
    inherit this repo's CLAUDE.md / .claude hooks / git state. Any documented option (e.g.
    ``system_prompt``, ``permission_mode``, ``can_use_tool``, ``hooks``) can be supplied via
    ``overrides``.
    """
    return ClaudeAgentOptions(model=model, cwd=str(cwd), setting_sources=[], **overrides)


def collect_assistant_text(messages: Iterable[object]) -> str:
    """Concatenate the text of every ``TextBlock`` across all ``AssistantMessage`` objects.

    This is the documented way to read a model's textual reply out of the message stream.
    """
    texts: list[str] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
    return "\n".join(texts)


async def collect_query_messages(sdk: ModuleType, prompt: str, options: ClaudeAgentOptions) -> list[object]:
    """Run the given SDK's ``query()`` to completion and return every message it yields.

    ``sdk`` is the implementation module under test -- either ``claude_agent_sdk`` or
    ``imbue.mngr_robinhood.agent_sdk`` -- supplied by the parametrized ``sdk`` fixture so each
    test runs against both targets.
    """
    return [message async for message in sdk.query(prompt=prompt, options=options)]


async def drain_response(client: ClaudeSDKClient) -> list[object]:
    """Consume one ``receive_response()`` turn and return every message it yields."""
    return [message async for message in client.receive_response()]


def find_result_message(messages: Iterable[object]) -> ResultMessage:
    """Return the single terminal ResultMessage from a message stream."""
    results = [message for message in messages if isinstance(message, ResultMessage)]
    assert len(results) == 1
    return results[0]
