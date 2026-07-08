import json
import urllib.request
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import HookContext
from claude_agent_sdk import HookInput
from claude_agent_sdk import HookJSONOutput
from claude_agent_sdk import HookMatcher
from claude_agent_sdk import PermissionResultAllow
from claude_agent_sdk import PermissionResultDeny
from claude_agent_sdk import ToolPermissionContext

from imbue.mngr_robinhood._agent_sdk.hook_bridge import HookBridge
from imbue.mngr_robinhood._agent_sdk.hook_bridge import _HookKind
from imbue.mngr_robinhood._agent_sdk.hook_bridge import _build_registry_and_settings_hooks
from imbue.mngr_robinhood._agent_sdk.hook_bridge import is_hook_bridge_needed
from imbue.mngr_robinhood._agent_sdk.hook_bridge import start_hook_bridge

_URL = "http://127.0.0.1:12345/hook"


async def _allow_can_use_tool(
    name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    return PermissionResultAllow(behavior="allow")


async def _noop_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
    return {}


def _discard_denial(denial: dict[str, Any]) -> None:
    return None


def _post_to_bridge(bridge: HookBridge, hook_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST a hook payload to a running bridge exactly as the agent's hook command would."""
    port = bridge.server.server_address[1]
    url = f"http://127.0.0.1:{port}/hook?hook_id={hook_id}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"))
    with urllib.request.urlopen(request, timeout=10.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _only_hook_id(bridge: HookBridge) -> str:
    return next(iter(bridge.registry.keys()))


def test_is_hook_bridge_needed() -> None:
    assert is_hook_bridge_needed(ClaudeAgentOptions()) is False
    assert is_hook_bridge_needed(ClaudeAgentOptions(can_use_tool=_allow_can_use_tool)) is True
    assert (
        is_hook_bridge_needed(
            ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_noop_hook])]})
        )
        is True
    )


def test_build_registry_maps_can_use_tool_to_catch_all_pre_tool_use() -> None:
    registry, settings_hooks = _build_registry_and_settings_hooks(
        ClaudeAgentOptions(can_use_tool=_allow_can_use_tool), _URL
    )
    assert list(settings_hooks.keys()) == ["PreToolUse"]
    entry = settings_hooks["PreToolUse"][0]
    assert entry["matcher"] == "*"
    assert len(registry) == 1
    (registration,) = registry.values()
    assert registration.kind == _HookKind.CAN_USE_TOOL
    assert registration.callback is _allow_can_use_tool


def test_build_registry_preserves_matcher_and_registers_each_hook() -> None:
    async def hook_a(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        return {}

    async def hook_b(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        return {}

    options = ClaudeAgentOptions(
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[hook_a]),
                HookMatcher(matcher="WebFetch", hooks=[hook_b]),
            ]
        }
    )
    registry, settings_hooks = _build_registry_and_settings_hooks(options, _URL)
    matchers = [entry.get("matcher") for entry in settings_hooks["PreToolUse"]]
    assert matchers == ["Bash", "WebFetch"]
    assert len(registry) == 2
    assert all(reg.kind == _HookKind.HOOK for reg in registry.values())


def test_build_registry_omits_matcher_key_when_none() -> None:
    options = ClaudeAgentOptions(hooks={"UserPromptSubmit": [HookMatcher(matcher=None, hooks=[_noop_hook])]})
    _registry, settings_hooks = _build_registry_and_settings_hooks(options, _URL)
    entry = settings_hooks["UserPromptSubmit"][0]
    assert "matcher" not in entry


def test_hook_command_is_valid_and_embeds_url_and_id() -> None:
    _registry, settings_hooks = _build_registry_and_settings_hooks(
        ClaudeAgentOptions(can_use_tool=_allow_can_use_tool), _URL
    )
    command = settings_hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert command.startswith("python3 -c ")
    assert _URL in command
    # The settings structure must be JSON-serializable (it is written to the --settings file).
    json.loads(json.dumps({"hooks": settings_hooks}))


def test_bridge_round_trip_can_use_tool_deny_records_denial() -> None:
    denials: list[dict[str, Any]] = []

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        assert isinstance(context, ToolPermissionContext)
        return PermissionResultDeny(behavior="deny", message="blocked", interrupt=False)

    bridge = start_hook_bridge(ClaudeAgentOptions(can_use_tool=can_use_tool), denials.append)
    try:
        result = _post_to_bridge(
            bridge, _only_hook_id(bridge), {"tool_name": "Bash", "tool_input": {"command": "x"}, "tool_use_id": "t1"}
        )
    finally:
        bridge.stop()
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "blocked"
    assert denials == [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "x"}}]


def test_bridge_round_trip_can_use_tool_allow_with_updated_input() -> None:
    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        rewritten = dict(tool_input)
        rewritten["command"] = "echo rewritten"
        return PermissionResultAllow(behavior="allow", updated_input=rewritten)

    bridge = start_hook_bridge(ClaudeAgentOptions(can_use_tool=can_use_tool), _discard_denial)
    try:
        result = _post_to_bridge(
            bridge, _only_hook_id(bridge), {"tool_name": "Bash", "tool_input": {"command": "echo original"}}
        )
    finally:
        bridge.stop()
    hook_specific = result["hookSpecificOutput"]
    assert hook_specific["permissionDecision"] == "allow"
    assert hook_specific["updatedInput"]["command"] == "echo rewritten"


def test_bridge_round_trip_hook_callback_returns_output() -> None:
    seen: list[str] = []

    async def pre_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        seen.append(str(input_data.get("tool_name", "")))
        return {}

    options = ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_hook])]})
    bridge = start_hook_bridge(options, _discard_denial)
    try:
        result = _post_to_bridge(
            bridge, _only_hook_id(bridge), {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}
        )
    finally:
        bridge.stop()
    assert result == {}
    assert seen == ["Bash"]


def test_bridge_dispatch_unknown_hook_id_returns_empty() -> None:
    bridge = start_hook_bridge(ClaudeAgentOptions(can_use_tool=_allow_can_use_tool), _discard_denial)
    try:
        result = _post_to_bridge(bridge, "does-not-exist", {"tool_name": "Bash"})
    finally:
        bridge.stop()
    assert result == {}


def test_bridge_preapproved_tool_allows_without_consulting_callback() -> None:
    consulted: list[str] = []

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        consulted.append(name)
        return PermissionResultDeny(behavior="deny", message="should not be consulted", interrupt=False)

    options = ClaudeAgentOptions(can_use_tool=can_use_tool, allowed_tools=["Bash"])
    bridge = start_hook_bridge(options, _discard_denial)
    try:
        result = _post_to_bridge(bridge, _only_hook_id(bridge), {"tool_name": "Bash", "tool_input": {"command": "x"}})
    finally:
        bridge.stop()
    # A pre-approved tool is allowed without ever calling the callback.
    assert consulted == []
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
