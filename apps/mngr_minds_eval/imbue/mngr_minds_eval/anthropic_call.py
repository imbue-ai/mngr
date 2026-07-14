"""One plain call to the Anthropic Messages API. Reads ANTHROPIC_API_KEY from the environment.

Deliberately minimal: the evaluator just hands Claude a prompt and gets text back.
"""

from __future__ import annotations

import anthropic
from anthropic.types import TextBlock

MODEL = "claude-opus-4-8"


def ask(prompt: str, *, max_tokens: int = 1024) -> str:
    """Send `prompt` as a single user message; return the concatenated text of the reply."""
    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from the env
    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content if isinstance(block, TextBlock))
