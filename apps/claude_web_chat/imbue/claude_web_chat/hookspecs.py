from collections.abc import Callable
from typing import Any

import pluggy
from fastapi import FastAPI

hookspec = pluggy.HookspecMarker("claude_web_chat")
hookimpl = pluggy.HookimplMarker("claude_web_chat")

EventBroadcaster = Callable[[str, dict[str, Any]], None]


class ClaudeWebChatHookSpec:
    @hookspec
    def endpoint(self, app: FastAPI) -> None:
        """Register additional endpoints on the FastAPI application."""

    @hookspec
    def register_event_broadcaster(self, broadcaster: EventBroadcaster) -> None:
        """Receive a reference to the event broadcaster for injecting events."""
