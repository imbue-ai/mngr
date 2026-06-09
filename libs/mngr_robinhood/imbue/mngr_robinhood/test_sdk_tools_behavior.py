"""Live verification that the agent can use built-in tools end-to-end through the SDK.

These assert observable outcomes (filesystem side effects, command output surfaced in the
stream) rather than which specific tool the model chooses, except where the prompt pins the
Bash tool explicitly. All tool-using turns run with ``permission_mode="bypassPermissions"`` in
an isolated ``cwd``.
"""

from pathlib import Path
from types import ModuleType

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ToolResultBlock
from claude_agent_sdk import ToolUseBlock
from claude_agent_sdk import UserMessage

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import drain_response
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def _run_tool_turn(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path, prompt: str) -> list[object]:
    options = make_sdk_options(sdk_live_model, sdk_cwd, permission_mode="bypassPermissions")
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        return await drain_response(client)


def _tool_use_blocks(messages: list[object]) -> list[ToolUseBlock]:
    return [b for m in messages if isinstance(m, AssistantMessage) for b in m.content if isinstance(b, ToolUseBlock)]


def _tool_result_blocks(messages: list[object]) -> list[ToolResultBlock]:
    return [
        b
        for m in messages
        if isinstance(m, UserMessage) and isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]


def _tool_result_text(messages: list[object]) -> str:
    """Concatenate the textual content of every tool result (handling str or block-list content)."""
    parts: list[str] = []
    for block in _tool_result_blocks(messages):
        parts.append(block.content if isinstance(block.content, str) else str(block.content))
    return "".join(parts)


async def test_bash_tool_creates_a_file(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    target = sdk_cwd / "created.txt"
    messages = await _run_tool_turn(
        sdk, sdk_live_model, sdk_cwd, f"Use the Bash tool to run exactly: echo CREATEDCONTENT > {target}"
    )
    assert find_result_message(messages).is_error is False
    assert target.exists()
    assert "CREATEDCONTENT" in target.read_text()


async def test_agent_reads_existing_file_contents(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    source = sdk_cwd / "to_read.txt"
    source.write_text("THE_MAGIC_TOKEN_4242\n")
    messages = await _run_tool_turn(
        sdk, sdk_live_model, sdk_cwd, f"Read the file {source} and tell me the exact token it contains."
    )
    assert "THE_MAGIC_TOKEN_4242" in collect_assistant_text(messages).upper()


async def test_agent_writes_file_with_requested_content(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    target = sdk_cwd / "note.txt"
    messages = await _run_tool_turn(
        sdk,
        sdk_live_model,
        sdk_cwd,
        f"Create a file at {target} whose entire contents are exactly: PERSISTED_VALUE_77",
    )
    assert find_result_message(messages).is_error is False
    assert target.exists()
    assert "PERSISTED_VALUE_77" in target.read_text()


async def test_agent_edits_existing_file(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    target = sdk_cwd / "editable.txt"
    target.write_text("the color is RED here\n")
    messages = await _run_tool_turn(
        sdk,
        sdk_live_model,
        sdk_cwd,
        f"In the file {target}, change the word RED to GREEN. Keep everything else the same.",
    )
    assert find_result_message(messages).is_error is False
    assert "GREEN" in target.read_text()


async def test_tool_use_and_result_ids_correlate(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await _run_tool_turn(sdk, sdk_live_model, sdk_cwd, "Use the Bash tool to run exactly: echo CORRELATE")
    use_ids = {block.id for block in _tool_use_blocks(messages)}
    result_blocks = _tool_result_blocks(messages)
    assert len(use_ids) >= 1
    assert len(result_blocks) >= 1
    # Every tool result must reference a tool use that actually occurred.
    assert all(block.tool_use_id in use_ids for block in result_blocks)


async def test_tool_result_content_contains_command_output(
    sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path
) -> None:
    messages = await _run_tool_turn(
        sdk, sdk_live_model, sdk_cwd, "Use the Bash tool to run exactly: echo OUTPUTTOKEN_9001"
    )
    assert "OUTPUTTOKEN_9001" in _tool_result_text(messages)


async def test_multiple_tool_calls_in_one_task(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    first = sdk_cwd / "one.txt"
    second = sdk_cwd / "two.txt"
    messages = await _run_tool_turn(
        sdk,
        sdk_live_model,
        sdk_cwd,
        f"Use the Bash tool to create two files: run `echo a > {first}` and then `echo b > {second}`.",
    )
    assert find_result_message(messages).is_error is False
    assert first.exists() and second.exists()
    assert len(_tool_use_blocks(messages)) >= 2


async def test_bash_tool_use_input_has_command_field(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await _run_tool_turn(sdk, sdk_live_model, sdk_cwd, "Use the Bash tool to run exactly: echo HASCOMMAND")
    bash_uses = [block for block in _tool_use_blocks(messages) if block.name == "Bash"]
    assert len(bash_uses) >= 1
    assert all("command" in block.input for block in bash_uses)


async def test_bash_pwd_reflects_configured_cwd(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await _run_tool_turn(sdk, sdk_live_model, sdk_cwd, "Use the Bash tool to run exactly: pwd")
    assert sdk_cwd.name in _tool_result_text(messages)


async def test_agent_lists_directory_contents(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    (sdk_cwd / "alpha_file.txt").write_text("a\n")
    (sdk_cwd / "beta_file.txt").write_text("b\n")
    messages = await _run_tool_turn(
        sdk, sdk_live_model, sdk_cwd, "List the files in the current directory and name them."
    )
    reply = collect_assistant_text(messages)
    assert "alpha_file.txt" in reply and "beta_file.txt" in reply


async def test_agent_finds_text_across_files(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    (sdk_cwd / "haystack.txt").write_text("nothing special here\nNEEDLE_TOKEN_5150 is here\nmore text\n")
    messages = await _run_tool_turn(
        sdk,
        sdk_live_model,
        sdk_cwd,
        "Search the files in the current directory for the text NEEDLE_TOKEN_5150 and report which file contains it.",
    )
    assert "haystack.txt" in collect_assistant_text(messages)


async def test_failing_bash_command_is_marked_as_error(sdk: ModuleType, sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await _run_tool_turn(sdk, sdk_live_model, sdk_cwd, "Use the Bash tool to run exactly: exit 7")
    result_blocks = _tool_result_blocks(messages)
    assert len(result_blocks) >= 1
    # The failing command must surface as an errored tool result.
    assert any(block.is_error is True for block in result_blocks)
