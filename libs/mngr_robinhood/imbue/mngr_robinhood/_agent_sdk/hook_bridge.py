"""Local HTTP bridge that turns claude settings-file hooks into in-process Python callbacks.

The real Agent SDK consults ``can_use_tool`` and ``hooks`` over claude's stdio control protocol.
The mngr transport drives claude interactively in tmux, so instead this bridge runs a tiny
``127.0.0.1`` HTTP server inside the SDK process and configures the mngr claude agent (via a
``--settings`` file) with hook commands that POST each hook event to the bridge. The bridge looks
up the registered Python callback, runs it on a dedicated anyio portal, and returns claude's hook
JSON output -- so ``can_use_tool`` (allow / deny / ``updated_input``) and the
``PreToolUse`` / ``PostToolUse`` / ``UserPromptSubmit`` hooks fire in-process, the same observable
contract as the real SDK.

``can_use_tool`` is mapped to a catch-all ``PreToolUse`` hook whose ``permissionDecision`` gates
the tool (claude's PreToolUse hooks can allow / deny / rewrite a tool call before it runs).
"""

import inspect
import json
import shlex
import shutil
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import auto
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from typing import Final
from urllib.parse import parse_qs
from urllib.parse import urlparse

from anyio.from_thread import BlockingPortal
from anyio.from_thread import start_blocking_portal
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import PermissionResultAllow
from claude_agent_sdk import PermissionResultDeny
from claude_agent_sdk import ToolPermissionContext
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# Hook commands block synchronously on the bridge round-trip; claude's default command-hook timeout
# is 10 minutes, so the bridge waits just under that for the Python callback to return.
_CALLBACK_TIMEOUT_SECONDS: Final[float] = 590.0

# The single bound path the hook command POSTs to (the registration is selected by ``?hook_id=``).
_BRIDGE_PATH: Final[str] = "/hook"


class _HookKind(UpperCaseStrEnum):
    """Whether a registration is a ``can_use_tool`` permission callback or a plain hook callback."""

    CAN_USE_TOOL = auto()
    HOOK = auto()


class _Registration(FrozenModel):
    """One registered callback addressed by a hook id embedded in the settings hook command."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: _HookKind = Field(description="Whether this is a can_use_tool or a plain hook callback")
    callback: SkipValidation[Callable[..., Any]] = Field(description="The user's async (or sync) callback")


async def _invoke_callback(callback: Callable[..., Any], args: tuple[Any, ...]) -> Any:
    """Call the (async or sync) callback and await it if needed; runs on the bridge's anyio portal."""
    outcome = callback(*args)
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


def _hook_command(url: str, hook_id: str) -> str:
    """Build the shell command claude runs for one hook: POST stdin to the bridge, echo its reply.

    Uses only the Python standard library (``urllib``) so it has no dependency beyond a ``python3``
    on the agent's PATH. Failures fail open (no output, exit 0) so a bridge hiccup never wedges the
    agent mid-turn.
    """
    script = (
        "import sys,urllib.request,urllib.error\n"
        "try:\n"
        "    body=sys.stdin.buffer.read()\n"
        f"    req=urllib.request.Request({url!r}+'?hook_id='+{hook_id!r},data=body,"
        "headers={'Content-Type':'application/json'})\n"
        f"    sys.stdout.write(urllib.request.urlopen(req,timeout={_CALLBACK_TIMEOUT_SECONDS!r}).read().decode())\n"
        "except (urllib.error.URLError, OSError, ValueError):\n"
        "    pass\n"
    )
    return f"python3 -c {shlex.quote(script)}"


def _build_registry_and_settings_hooks(
    options: ClaudeAgentOptions, url: str
) -> tuple[dict[str, _Registration], dict[str, list[dict[str, Any]]]]:
    """Translate the options' ``hooks`` + ``can_use_tool`` into a registry and claude settings hooks."""
    registry: dict[str, _Registration] = {}
    settings_hooks: dict[str, list[dict[str, Any]]] = {}
    next_index = 0

    # Each HookMatcher's callbacks become individual settings hook entries (so two matchers in one
    # event both fire, and a non-matching matcher simply never invokes its callback).
    if options.hooks:
        for event_name, matchers in options.hooks.items():
            for matcher in matchers:
                for callback in matcher.hooks:
                    hook_id = f"hook-{next_index}"
                    next_index += 1
                    registry[hook_id] = _Registration(kind=_HookKind.HOOK, callback=callback)
                    entry: dict[str, Any] = {
                        "hooks": [{"type": "command", "command": _hook_command(url, hook_id), "timeout": 600}]
                    }
                    if matcher.matcher is not None:
                        entry["matcher"] = matcher.matcher
                    settings_hooks.setdefault(str(event_name), []).append(entry)

    # can_use_tool is consulted for every tool via a catch-all PreToolUse hook.
    if options.can_use_tool is not None:
        hook_id = f"perm-{next_index}"
        registry[hook_id] = _Registration(kind=_HookKind.CAN_USE_TOOL, callback=options.can_use_tool)
        settings_hooks.setdefault("PreToolUse", []).append(
            {"matcher": "*", "hooks": [{"type": "command", "command": _hook_command(url, hook_id), "timeout": 600}]}
        )

    return registry, settings_hooks


class _BridgeServer(ThreadingHTTPServer):
    """A threading HTTP server carrying a reference to its owning :class:`HookBridge`."""

    bridge: "HookBridge"


class _BridgeHandler(BaseHTTPRequestHandler):
    """Per-request handler that delegates to the owning :class:`HookBridge` on its server."""

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            # A malformed Content-Length is treated as no body so the handler never crashes.
            content_length = 0
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""
        query = parse_qs(urlparse(self.path).query)
        hook_id_values = query.get("hook_id", [])
        hook_id = hook_id_values[0] if hook_id_values else ""
        bridge = self.server.bridge  # ty: ignore[unresolved-attribute]
        response = bridge.dispatch(hook_id, raw_body)
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        # Silence the default stderr access logging; bridge activity is logged via loguru instead.
        pass


class HookBridge(MutableModel):
    """Owns the localhost HTTP server, the anyio portal, the registry, and the temp settings file."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    server: SkipValidation[_BridgeServer] = Field(description="The bound localhost HTTP server")
    server_thread: SkipValidation[Thread] = Field(description="Thread running the server's serve_forever loop")
    portal: SkipValidation[BlockingPortal] = Field(description="anyio portal the callbacks run on")
    portal_context: SkipValidation[AbstractContextManager[BlockingPortal]] = Field(
        description="Context manager owning the portal's event-loop thread"
    )
    registry: dict[str, _Registration] = Field(description="hook id -> registered callback")
    record_denial: SkipValidation[Callable[[dict[str, Any]], None]] = Field(
        description="Sink for permission denials, surfaced in ResultMessage.permission_denials"
    )
    preapproved_tools: frozenset[str] = Field(
        description="Tools pre-approved via allowed_tools; can_use_tool is not consulted for these"
    )
    settings_dir: Path = Field(description="Temp dir holding the hooks --settings file")
    settings_path: Path = Field(description="Path to the hooks --settings JSON file")

    def dispatch(self, hook_id: str, raw_body: bytes) -> dict[str, Any]:
        """Route one hook POST to its callback and return claude's hook JSON output."""
        registration = self.registry.get(hook_id)
        if registration is None:
            return {}
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Hook bridge received malformed payload for {}: {}", hook_id, exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        try:
            if registration.kind == _HookKind.CAN_USE_TOOL:
                return self._dispatch_permission(registration.callback, payload)
            return self._dispatch_hook(registration.callback, payload)
        except (TimeoutError, RuntimeError, OSError) as exc:
            # Fail open so a callback error never wedges the agent's turn.
            logger.opt(exception=exc).warning("Hook bridge callback failed for {}; failing open", hook_id)
            return {}

    def _dispatch_permission(self, callback: Callable[..., Any], payload: dict[str, Any]) -> dict[str, Any]:
        tool_name = payload.get("tool_name", "")
        # A tool pre-approved via allowed_tools must run without consulting can_use_tool (the real SDK
        # only consults the callback for non-pre-approved tools); allow it without calling the callback.
        if tool_name in self.preapproved_tools:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
        raw_tool_input = payload.get("tool_input")
        tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
        tool_use_id = payload.get("tool_use_id")
        context = ToolPermissionContext(suggestions=[], tool_use_id=tool_use_id)
        result = self.portal.call(_invoke_callback, callback, (tool_name, tool_input, context))
        if isinstance(result, PermissionResultDeny):
            self.record_denial({"tool_name": tool_name, "tool_use_id": tool_use_id, "tool_input": tool_input})
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": result.message or "denied by can_use_tool",
                }
            }
        if isinstance(result, PermissionResultAllow):
            hook_specific: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
            if result.updated_input is not None:
                hook_specific["updatedInput"] = result.updated_input
            return {"hookSpecificOutput": hook_specific}
        return {}

    def _dispatch_hook(self, callback: Callable[..., Any], payload: dict[str, Any]) -> dict[str, Any]:
        tool_use_id = payload.get("tool_use_id")
        hook_context: dict[str, Any] = {"signal": None}
        result = self.portal.call(_invoke_callback, callback, (payload, tool_use_id, hook_context))
        return result if isinstance(result, dict) else {}

    def stop(self) -> None:
        """Shut down the server, stop the portal loop, and remove the temp settings dir."""
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5.0)
        self.portal_context.__exit__(None, None, None)
        shutil.rmtree(self.settings_dir, ignore_errors=True)


def is_hook_bridge_needed(options: ClaudeAgentOptions) -> bool:
    """True if the options request in-process callbacks that the bridge must serve."""
    return bool(options.hooks) or options.can_use_tool is not None


def start_hook_bridge(
    options: ClaudeAgentOptions,
    record_denial: Callable[[dict[str, Any]], None],
) -> HookBridge:
    """Bind the localhost server, start the anyio portal, write the ``--settings`` file, and serve."""
    server = _BridgeServer(("127.0.0.1", 0), _BridgeHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}{_BRIDGE_PATH}"
    registry, settings_hooks = _build_registry_and_settings_hooks(options, url)
    settings_dir = Path(tempfile.mkdtemp(prefix="mngr-sdk-hooks-"))
    settings_path = settings_dir / "hook_settings.json"
    settings_path.write_text(json.dumps({"hooks": settings_hooks}, indent=2) + "\n")
    portal_context = start_blocking_portal()
    portal = portal_context.__enter__()
    server_thread = Thread(target=server.serve_forever, name="mngr-sdk-hook-bridge", daemon=True)
    bridge = HookBridge(
        server=server,
        server_thread=server_thread,
        portal=portal,
        portal_context=portal_context,
        registry=registry,
        record_denial=record_denial,
        preapproved_tools=frozenset(options.allowed_tools or ()),
        settings_dir=settings_dir,
        settings_path=settings_path,
    )
    server.bridge = bridge
    server_thread.start()
    return bridge
