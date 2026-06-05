"""Shared helpers for the live Claude Agent SDK test suite (test_sdk_*.py).

These factor out the two pieces of setup/validation that are otherwise copy-pasted across the
SDK test files: building the hermetic ``ClaudeAgentOptions`` and collecting assistant text from a
message stream. They are non-fixture utilities, so they live here (per the project's testing
conventions) rather than in conftest.py.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ClaudeAgentOptions
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
