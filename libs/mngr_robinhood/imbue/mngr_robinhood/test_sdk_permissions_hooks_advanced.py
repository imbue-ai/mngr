"""Live verification of advanced documented permission-callback and hook behavior.

Covers ``can_use_tool`` input rewriting and deny/interrupt semantics, the ``PreToolUse`` /
``PostToolUse`` / ``UserPromptSubmit`` hook events, hook matchers, and the ``plan`` permission
mode. Tool gating requires ``permission_mode="default"`` (so the callback is consulted) and the
tool must NOT be pre-approved via ``allowed_tools``.
"""

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import HookContext
from claude_agent_sdk import HookInput
from claude_agent_sdk import HookJSONOutput
from claude_agent_sdk import HookMatcher
from claude_agent_sdk import PermissionResultAllow
from claude_agent_sdk import PermissionResultDeny
from claude_agent_sdk import ToolPermissionContext

from imbue.mngr_robinhood.testing import collect_query_messages
from imbue.mngr_robinhood.testing import drain_response
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def _run_client(sdk: ModuleType, options: ClaudeAgentOptions, prompt: str) -> list[object]:
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        return await drain_response(client)


async def test_can_use_tool_updated_input_rewrites_command(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    redirected = sdk_cwd / "redirected.txt"
    original = sdk_cwd / "original.txt"

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        rewritten = dict(tool_input)
        rewritten["command"] = f"echo REWRITTEN > {redirected}"
        return PermissionResultAllow(behavior="allow", updated_input=rewritten)

    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="default", can_use_tool=can_use_tool)
    await _run_client(sdk, options, f"Use the Bash tool to run exactly: echo ORIGINAL > {original}")

    assert redirected.exists()
    assert not original.exists()


async def test_can_use_tool_deny_without_interrupt_completes_run(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    marker = sdk_cwd / "denied_noint.txt"

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultDeny:
        return PermissionResultDeny(behavior="deny", message="not allowed", interrupt=False)

    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="default", can_use_tool=can_use_tool)
    messages = await _run_client(sdk, options, f"Use the Bash tool to run exactly: echo x > {marker}")

    # A plain deny lets the run finish normally; the side effect must not happen.
    assert find_result_message(messages).subtype == "success"
    assert not marker.exists()


async def test_can_use_tool_receives_command_input_and_context(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    seen_inputs: list[dict[str, Any]] = []
    seen_context_types: list[bool] = []

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        seen_inputs.append(tool_input)
        seen_context_types.append(isinstance(context, ToolPermissionContext))
        return PermissionResultAllow(behavior="allow")

    # A file-writing command requires permission, so the callback is consulted (a bare side-effect-free
    # echo would be auto-approved and never reach can_use_tool).
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="default", can_use_tool=can_use_tool)
    await _run_client(sdk, options, f"Use the Bash tool to run exactly: echo CONTEXTCHECK > {sdk_cwd / 'ctx.txt'}")

    assert len(seen_inputs) >= 1
    assert any("command" in tool_input for tool_input in seen_inputs)
    assert all(seen_context_types)


async def test_can_use_tool_consulted_once_for_single_command(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    bash_calls: list[str] = []

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        if name == "Bash":
            bash_calls.append(name)
        return PermissionResultAllow(behavior="allow")

    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="default", can_use_tool=can_use_tool)
    await _run_client(sdk, options, f"Use the Bash tool exactly once to run: echo ONCE > {sdk_cwd / 'once.txt'}")

    assert len(bash_calls) == 1


async def test_pre_tool_use_hook_receives_tool_name_and_input(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    seen: list[dict[str, Any]] = []

    async def pre_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        seen.append(dict(input_data))
        return {}

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="bypassPermissions",
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_hook])]},
    )
    await _run_client(sdk, options, "Use the Bash tool to run exactly: echo HOOKINPUT")

    assert any(entry.get("tool_name") == "Bash" for entry in seen)
    assert any("tool_input" in entry for entry in seen)


async def test_post_tool_use_hook_fires_after_tool(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    seen: list[str] = []

    async def post_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        seen.append(str(input_data.get("tool_name", "")))
        return {}

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="bypassPermissions",
        hooks={"PostToolUse": [HookMatcher(matcher="Bash", hooks=[post_hook])]},
    )
    await _run_client(sdk, options, "Use the Bash tool to run exactly: echo POSTHOOK")

    assert "Bash" in seen


async def test_pre_and_post_hooks_both_fire(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    events: list[str] = []

    async def pre_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        events.append("pre")
        return {}

    async def post_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        events.append("post")
        return {}

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_hook])],
            "PostToolUse": [HookMatcher(matcher="Bash", hooks=[post_hook])],
        },
    )
    await _run_client(sdk, options, "Use the Bash tool to run exactly: echo BOTHHOOKS")

    assert "pre" in events
    assert "post" in events


async def test_hook_matcher_does_not_fire_for_non_matching_tool(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    seen: list[str] = []

    async def pre_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        seen.append(str(input_data.get("tool_name", "")))
        return {}

    # The matcher targets a tool the task will not use, so the hook must never fire.
    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="bypassPermissions",
        hooks={"PreToolUse": [HookMatcher(matcher="WebFetch", hooks=[pre_hook])]},
    )
    await _run_client(sdk, options, "Use the Bash tool to run exactly: echo NOMATCH")

    assert seen == []


async def test_pre_tool_use_hook_can_deny_via_permission_decision(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    marker = sdk_cwd / "hook_denied.txt"

    async def deny_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "blocked by test hook",
            }
        }

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="default",
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[deny_hook])]},
    )
    messages = await _run_client(sdk, options, f"Use the Bash tool to run exactly: echo x > {marker}")

    assert find_result_message(messages).subtype == "success"
    assert not marker.exists()


async def test_user_prompt_submit_hook_fires_with_prompt(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    seen: list[dict[str, Any]] = []

    async def prompt_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        seen.append(dict(input_data))
        return {}

    options = make_sdk_options(
        sdk_live_model, sdk_cwd, hooks={"UserPromptSubmit": [HookMatcher(matcher=None, hooks=[prompt_hook])]}
    )
    await collect_query_messages(sdk, "Reply with UNIQUEPROMPTTOKEN.", options)

    assert any(entry.get("hook_event_name") == "UserPromptSubmit" for entry in seen)
    assert any("UNIQUEPROMPTTOKEN" in str(entry.get("prompt", "")) for entry in seen)


async def test_two_hook_matchers_in_one_event_both_fire(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    fired: list[str] = []

    async def hook_a(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        fired.append("a")
        return {}

    async def hook_b(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        fired.append("b")
        return {}

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[hook_a]),
                HookMatcher(matcher="Bash", hooks=[hook_b]),
            ]
        },
    )
    await _run_client(sdk, options, "Use the Bash tool to run exactly: echo TWOMATCHERS")

    assert "a" in fired and "b" in fired


async def test_plan_mode_prevents_tool_execution(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    marker = sdk_cwd / "plan_should_not_exist.txt"
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="plan")
    messages = await collect_query_messages(
        sdk, f"Use the Bash tool to create a file by running: echo x > {marker}", options
    )
    assert find_result_message(messages).subtype == "success"
    assert not marker.exists()


async def test_can_use_tool_deny_records_permission_denial(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultDeny:
        return PermissionResultDeny(behavior="deny", message="denied", interrupt=False)

    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="default", can_use_tool=can_use_tool)
    messages = await _run_client(
        sdk, options, f"Use the Bash tool to run exactly: echo DENYRECORD > {sdk_cwd / 'deny.txt'}"
    )
    result = find_result_message(messages)
    assert result.permission_denials is not None
    assert any(denial.get("tool_name") == "Bash" for denial in result.permission_denials)


async def test_user_prompt_submit_hook_with_null_matcher_fires(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    fired: list[bool] = []

    async def prompt_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        fired.append(True)
        return {}

    options = make_sdk_options(
        sdk_live_model, sdk_cwd, hooks={"UserPromptSubmit": [HookMatcher(matcher=None, hooks=[prompt_hook])]}
    )
    await collect_query_messages(sdk, "Say hi.", options)
    assert len(fired) >= 1


async def test_pre_tool_use_hook_observes_command_in_input(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    seen_commands: list[str] = []

    async def pre_hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        tool_input = input_data.get("tool_input", {})
        if isinstance(tool_input, dict):
            seen_commands.append(str(tool_input.get("command", "")))
        return {}

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="bypassPermissions",
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_hook])]},
    )
    await _run_client(sdk, options, "Use the Bash tool to run exactly: echo COMMANDINHOOK")
    assert any("COMMANDINHOOK" in command for command in seen_commands)


async def test_can_use_tool_allow_lets_multiple_distinct_tools_run(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    first = sdk_cwd / "multi_a.txt"
    second = sdk_cwd / "multi_b.txt"
    allowed_tools_seen: list[str] = []

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        allowed_tools_seen.append(name)
        return PermissionResultAllow(behavior="allow")

    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="default", can_use_tool=can_use_tool)
    await _run_client(
        sdk,
        options,
        f"Use the Bash tool to run `echo a > {first}` and then `echo b > {second}`.",
    )
    assert first.exists() and second.exists()
    assert allowed_tools_seen.count("Bash") >= 1
