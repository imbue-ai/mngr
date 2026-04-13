from collections.abc import Callable
from typing import Any

import pluggy
from fastapi import FastAPI

hookspec = pluggy.HookspecMarker("minds_workspace_server")
hookimpl = pluggy.HookimplMarker("minds_workspace_server")

EventBroadcaster = Callable[[str, dict[str, Any]], None]


class MindsWorkspaceServerHookSpec:
    @hookspec
    def endpoint(self, app: FastAPI) -> None:
        """Register additional endpoints on the FastAPI application."""

    @hookspec
    def register_event_broadcaster(self, broadcaster: EventBroadcaster) -> None:
        """Receive a reference to the event broadcaster for injecting events."""
