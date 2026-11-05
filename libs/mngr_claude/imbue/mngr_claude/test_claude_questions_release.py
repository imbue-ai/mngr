from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import record_one_proxied_turn

_QUESTION_TOOL: Final[str] = "AskUserQuestion"
_DISALLOW_CONSUMER: Final[str] = (
    "ClaudeAgent.assemble_command, which passes `--disallowed-tools AskUserQuestion` for auto_disable_questions"
)


def _tools_offered_in_one_turn(tmp_path: Path, *, is_questions_disabled: bool) -> tuple[set[str], str]:
    """Run one turn on a fresh proxied agent; return the tools its /v1/messages requests offered, and a drift note."""
    settings_toml = "\n[agent_types.claude]\nauto_disable_questions = true\n" if is_questions_disabled else ""
    bodies, drift = record_one_proxied_turn(tmp_path, settings_toml, "the question tool")
    tool_lists = [body["tools"] for body in bodies if "tools" in body]
    assert tool_lists, f"the agent sent no /v1/messages request carrying a tool list {drift}"
    return {str(tool.get("name")) for tools in tool_lists for tool in tools if isinstance(tool, dict)}, drift


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_agent_without_questions_is_not_offered_the_question_tool(tmp_path: Path) -> None:
    """An agent with auto_disable_questions is never offered claude's question tool, so it cannot block on one.

    Pins the tool's name, ``_QUESTION_TOOL``, which mngr disallows by name.
    """
    reason = ClaudeReleaseProfile().unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    default_tools, drift = _tools_offered_in_one_turn(tmp_path / "default", is_questions_disabled=False)
    assert _QUESTION_TOOL in default_tools, (
        f"claude no longer offers a tool named {_QUESTION_TOOL!r} {drift}; offered: {sorted(default_tools)}; "
        f"consumer: {_DISALLOW_CONSUMER}"
    )
    no_question_tools, _ = _tools_offered_in_one_turn(tmp_path / "no-questions", is_questions_disabled=True)
    assert _QUESTION_TOOL not in no_question_tools, (
        f"an agent with auto_disable_questions is still offered {_QUESTION_TOOL!r} {drift}; "
        f"consumer: {_DISALLOW_CONSUMER}"
    )
