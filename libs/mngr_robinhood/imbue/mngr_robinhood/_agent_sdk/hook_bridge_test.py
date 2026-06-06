import json
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import HookMatcher
from claude_agent_sdk import ToolPermissionContext

from imbue.mngr_robinhood._agent_sdk.hook_bridge import _HookKind
from imbue.mngr_robinhood._agent_sdk.hook_bridge import _build_registry_and_settings_hooks
from imbue.mngr_robinhood._agent_sdk.hook_bridge import is_hook_bridge_needed

_URL = "http://127.0.0.1:12345/hook"


def test_is_hook_bridge_needed() -> None:
    assert is_hook_bridge_needed(ClaudeAgentOptions()) is False
    assert is_hook_bridge_needed(ClaudeAgentOptions(can_use_tool=lambda *a: None)) is True
    assert (
        is_hook_bridge_needed(
            ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[lambda: None])]})
        )
        is True
    )


def test_build_registry_maps_can_use_tool_to_catch_all_pre_tool_use() -> None:
    async def can_use_tool(name: str, tool_input: dict[str, Any], context: ToolPermissionContext) -> None:
        return None

    registry, settings_hooks = _build_registry_and_settings_hooks(ClaudeAgentOptions(can_use_tool=can_use_tool), _URL)
    assert list(settings_hooks.keys()) == ["PreToolUse"]
    entry = settings_hooks["PreToolUse"][0]
    assert entry["matcher"] == "*"
    assert len(registry) == 1
    (registration,) = registry.values()
    assert registration.kind == _HookKind.CAN_USE_TOOL
    assert registration.callback is can_use_tool


def test_build_registry_preserves_matcher_and_registers_each_hook() -> None:
    async def hook_a() -> dict[str, Any]:
        return {}

    async def hook_b() -> dict[str, Any]:
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
    async def prompt_hook() -> dict[str, Any]:
        return {}

    options = ClaudeAgentOptions(hooks={"UserPromptSubmit": [HookMatcher(matcher=None, hooks=[prompt_hook])]})
    _registry, settings_hooks = _build_registry_and_settings_hooks(options, _URL)
    entry = settings_hooks["UserPromptSubmit"][0]
    assert "matcher" not in entry


def test_hook_command_is_valid_and_embeds_url_and_id() -> None:
    async def can_use_tool(name: str, tool_input: dict[str, Any], context: ToolPermissionContext) -> None:
        return None

    _registry, settings_hooks = _build_registry_and_settings_hooks(ClaudeAgentOptions(can_use_tool=can_use_tool), _URL)
    command = settings_hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert command.startswith("python3 -c ")
    assert _URL in command
    # The settings structure must be JSON-serializable (it is written to the --settings file).
    json.loads(json.dumps({"hooks": settings_hooks}))
