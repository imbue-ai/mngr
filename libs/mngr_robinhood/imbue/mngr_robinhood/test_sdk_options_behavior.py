"""Live verification of observable, behavior-affecting ``ClaudeAgentOptions`` fields.

Covers ``env``, ``allowed_tools`` / ``disallowed_tools``, ``permission_mode`` (bypass/accept),
pinned ``model``, ``system_prompt`` (string and preset), ``add_dirs``, and ``cwd``.
"""

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import PermissionResultAllow
from claude_agent_sdk import ToolPermissionContext

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import collect_query_messages
from imbue.mngr_robinhood.testing import drain_response
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def test_env_var_is_visible_to_bash(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = make_sdk_options(
        sdk_live_model, sdk_cwd, permission_mode="bypassPermissions", env={"PROBE_TOKEN_VAR": "ENVVALUE7777"}
    )
    messages = await collect_query_messages(sdk, "Use the Bash tool to run exactly: echo $PROBE_TOKEN_VAR", options)
    result = find_result_message(messages)
    assert result.is_error is False
    assert "ENVVALUE7777" in (result.result or "")


async def test_allowed_tools_preapproves_without_consulting_callback(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    callback_calls: list[str] = []
    marker = sdk_cwd / "preapproved.txt"

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        callback_calls.append(name)
        return PermissionResultAllow(behavior="allow")

    options = make_sdk_options(
        sdk_live_model, sdk_cwd, permission_mode="default", allowed_tools=["Bash"], can_use_tool=can_use_tool
    )
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(f"Use the Bash tool to run exactly: echo ok > {marker}")
        await drain_response(client)

    # A pre-approved tool must run without ever consulting the permission callback.
    assert callback_calls == []
    assert marker.exists()


async def test_disallowed_tools_prevents_tool_use(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    marker = sdk_cwd / "blocked.txt"
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="bypassPermissions", disallowed_tools=["Bash"])
    messages = await collect_query_messages(sdk, f"Use the Bash tool to run exactly: echo x > {marker}", options)
    assert find_result_message(messages).is_error is False
    assert not marker.exists()


async def test_disallowed_tool_still_answers_plain_questions(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd, disallowed_tools=["Bash"])
    messages = await collect_query_messages(sdk, "What is 2 plus 2? Answer with just the number.", options)
    assert find_result_message(messages).is_error is False
    assert "4" in collect_assistant_text(messages)


async def test_bypass_permissions_runs_tool_without_callback(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    marker = sdk_cwd / "bypassed.txt"
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="bypassPermissions")
    messages = await collect_query_messages(sdk, f"Use the Bash tool to run exactly: echo ok > {marker}", options)
    assert find_result_message(messages).is_error is False
    assert marker.exists()


async def test_accept_edits_allows_file_creation(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    marker = sdk_cwd / "accepted.txt"
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="acceptEdits")
    messages = await collect_query_messages(
        sdk, f"Create a file at {marker} whose contents are exactly: ACCEPTED_EDIT_VALUE", options
    )
    assert find_result_message(messages).is_error is False
    assert marker.exists()


async def test_pinned_model_id_is_used(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = ClaudeAgentOptions(model="claude-haiku-4-5", cwd=str(sdk_cwd), setting_sources=[])
    messages = await collect_query_messages(sdk, "Say hi.", options)
    assistant_models = [m.model for m in messages if isinstance(m, AssistantMessage)]
    assert len(assistant_models) >= 1
    assert all("haiku" in model.lower() for model in assistant_models)


async def test_system_prompt_string_replaces_default(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        system_prompt="You are a fixture that responds to every message with exactly the single word ROBOTREPLY.",
    )
    messages = await collect_query_messages(sdk, "Tell me about the weather.", options)
    assert "ROBOTREPLY" in collect_assistant_text(messages).upper()


async def test_system_prompt_preset_with_append(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # The documented SystemPromptPreset form, with an appended instruction we can observe.
    options = make_sdk_options(
        sdk_live_model,
        sdk_cwd,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "You must end every response with the exact marker token APPENDMARKER42.",
        },
    )
    messages = await collect_query_messages(sdk, "Say hello in one short sentence.", options)
    assert "APPENDMARKER42" in collect_assistant_text(messages).upper()


async def test_cwd_accepts_path_object(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    # cwd is documented as ``str | Path``; pass a Path directly.
    options = ClaudeAgentOptions(model=sdk_live_model, cwd=sdk_cwd, setting_sources=[])
    messages = await collect_query_messages(sdk, "Reply with exactly the word PATHCWD.", options)
    assert find_result_message(messages).is_error is False
    assert "PATHCWD" in collect_assistant_text(messages).upper()


async def test_relative_path_write_lands_in_cwd(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="bypassPermissions")
    messages = await collect_query_messages(
        sdk, "Use the Bash tool to run exactly: echo relativeok > relative_output.txt", options
    )
    assert find_result_message(messages).is_error is False
    assert (sdk_cwd / "relative_output.txt").exists()


async def test_add_dirs_makes_external_directory_accessible(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path, tmp_path: Path
) -> None:
    external_dir = tmp_path / "external_workspace"
    external_dir.mkdir()
    secret_file = external_dir / "secret.txt"
    secret_file.write_text("EXTERNAL_DIR_TOKEN_8888\n")

    options = make_sdk_options(
        sdk_live_model, sdk_cwd, permission_mode="bypassPermissions", add_dirs=[str(external_dir)]
    )
    messages = await collect_query_messages(
        sdk, f"Read the file {secret_file} and tell me the exact token it contains.", options
    )
    assert "EXTERNAL_DIR_TOKEN_8888" in collect_assistant_text(messages).upper()
