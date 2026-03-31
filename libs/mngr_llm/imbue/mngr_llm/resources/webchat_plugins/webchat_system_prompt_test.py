"""Tests for the system prompt plugin."""

from __future__ import annotations

from pathlib import Path

from imbue.mngr_llm.resources.webchat_plugins.webchat_system_prompt import SystemPromptPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_system_prompt import create_system_prompt_plugin


def test_modify_command_injects_system_flag() -> None:
    plugin = SystemPromptPlugin(system_prompt="You are helpful.")
    command = ["llm", "-m", "gpt-4", "--cid", "abc", "hello"]
    plugin.modify_llm_prompt_command(command)
    assert command == ["llm", "-m", "gpt-4", "--cid", "abc", "hello", "--system", "You are helpful."]


def test_modify_command_skips_when_system_already_present() -> None:
    plugin = SystemPromptPlugin(system_prompt="You are helpful.")
    command = ["llm", "-m", "gpt-4", "--system", "existing prompt", "hello"]
    plugin.modify_llm_prompt_command(command)
    assert command == ["llm", "-m", "gpt-4", "--system", "existing prompt", "hello"]


def test_modify_command_skips_when_short_system_flag_present() -> None:
    plugin = SystemPromptPlugin(system_prompt="You are helpful.")
    command = ["llm", "-m", "gpt-4", "-s", "existing prompt", "hello"]
    plugin.modify_llm_prompt_command(command)
    assert command == ["llm", "-m", "gpt-4", "-s", "existing prompt", "hello"]


def test_modify_command_skips_when_no_system_prompt() -> None:
    plugin = SystemPromptPlugin(system_prompt=None)
    command = ["llm", "-m", "gpt-4", "hello"]
    plugin.modify_llm_prompt_command(command)
    assert command == ["llm", "-m", "gpt-4", "hello"]


def test_create_plugin_from_global_md(tmp_path: Path) -> None:
    (tmp_path / "GLOBAL.md").write_text("You are a mind.")
    plugin = create_system_prompt_plugin(str(tmp_path))
    assert plugin is not None
    assert plugin.system_prompt == "You are a mind."


def test_create_plugin_from_talking_prompt(tmp_path: Path) -> None:
    (tmp_path / "talking").mkdir()
    (tmp_path / "talking" / "PROMPT.md").write_text("Be conversational.")
    plugin = create_system_prompt_plugin(str(tmp_path))
    assert plugin is not None
    assert plugin.system_prompt == "Be conversational."


def test_create_plugin_concatenates_both_files(tmp_path: Path) -> None:
    (tmp_path / "GLOBAL.md").write_text("Global context.")
    (tmp_path / "talking").mkdir()
    (tmp_path / "talking" / "PROMPT.md").write_text("Talking style.")
    plugin = create_system_prompt_plugin(str(tmp_path))
    assert plugin is not None
    assert plugin.system_prompt == "Global context.\n\nTalking style."


def test_create_plugin_returns_none_when_no_files(tmp_path: Path) -> None:
    plugin = create_system_prompt_plugin(str(tmp_path))
    assert plugin is None


def test_create_plugin_returns_none_when_empty_dir() -> None:
    plugin = create_system_prompt_plugin("")
    assert plugin is None
