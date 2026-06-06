"""Live verification of the documented ``can_use_tool`` permission callback and ``hooks``.

The ``can_use_tool`` callback is only consulted when the tool is not pre-approved and the
permission mode forces gating, so these tests use ``permission_mode="default"`` and do NOT list
the tool in ``allowed_tools``. Allow/deny is verified by an observable filesystem side effect
(a marker file written inside the isolated ``cwd``).
"""

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk import HookContext
from claude_agent_sdk import HookInput
from claude_agent_sdk import HookJSONOutput
from claude_agent_sdk import HookMatcher
from claude_agent_sdk import PermissionResultAllow
from claude_agent_sdk import PermissionResultDeny
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import ToolPermissionContext

from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def _drain_to_result(client: ClaudeSDKClient) -> ResultMessage:
    """Consume one response turn and return its terminal ResultMessage."""
    result: ResultMessage | None = None
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            result = message
    assert result is not None
    return result


async def test_can_use_tool_allow_lets_the_tool_run(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    observed_tool_names: list[str] = []
    marker_file = sdk_cwd / "ALLOW_MARKER.txt"

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        observed_tool_names.append(tool_name)
        return PermissionResultAllow(behavior="allow")

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="default",
        can_use_tool=can_use_tool,
    )
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(f"Use the Bash tool to run exactly: echo allowed > {marker_file}")
        result = await _drain_to_result(client)

    # The callback must have been consulted for the Bash tool, and allowing it must let it run.
    assert "Bash" in observed_tool_names
    assert result.is_error is False
    assert marker_file.exists()


async def test_can_use_tool_deny_blocks_the_tool(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    observed_tool_names: list[str] = []
    marker_file = sdk_cwd / "DENY_MARKER.txt"

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultDeny:
        observed_tool_names.append(tool_name)
        return PermissionResultDeny(behavior="deny", message="denied by test", interrupt=False)

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="default",
        can_use_tool=can_use_tool,
    )
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(f"Use the Bash tool to run exactly: echo denied > {marker_file}")
        result = await _drain_to_result(client)

    # Denying must prevent the side effect and be recorded in the documented permission_denials field.
    assert "Bash" in observed_tool_names
    assert not marker_file.exists()
    assert result.permission_denials is not None
    assert len(result.permission_denials) >= 1


async def test_pre_tool_use_hook_observes_tool_call(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    observed_tool_names: list[str] = []
    marker_file = sdk_cwd / "HOOK_MARKER.txt"

    async def pre_tool_use_hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        observed_tool_names.append(str(input_data.get("tool_name", "")))
        empty_output: HookJSONOutput = {}
        return empty_output

    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        permission_mode="bypassPermissions",
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_tool_use_hook])]},
    )
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(f"Use the Bash tool to run exactly: echo hooked > {marker_file}")
        result = await _drain_to_result(client)

    # The PreToolUse hook must fire for the matched tool before it runs.
    assert "Bash" in observed_tool_names
    assert result.is_error is False
    assert marker_file.exists()
