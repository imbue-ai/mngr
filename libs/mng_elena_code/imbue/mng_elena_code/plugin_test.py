"""Unit tests for the mng_elena_code plugin."""

import shlex
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

import pluggy

from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.api.test_fixtures import FakeHost
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import HostId
from imbue.mng_claude_zygote.plugin import AGENT_TTYD_COMMAND
from imbue.mng_claude_zygote.plugin import AGENT_TTYD_WINDOW_NAME
from imbue.mng_claude_zygote.plugin import ClaudeZygoteAgent
from imbue.mng_claude_zygote.plugin import ClaudeZygoteConfig
from imbue.mng_elena_code.plugin import ELENA_SYSTEM_PROMPT
from imbue.mng_elena_code.plugin import ElenaCodeAgent
from imbue.mng_elena_code.plugin import _merge_system_prompt_into_args
from imbue.mng_elena_code.plugin import override_command_options
from imbue.mng_elena_code.plugin import register_agent_type


class _DummyCommandClass:
    pass


def _make_elena_agent(tmp_path: Path) -> tuple[ElenaCodeAgent, OnlineHostInterface]:
    """Create an ElenaCodeAgent with minimal dependencies for testing assemble_command."""
    pm = pluggy.PluginManager("mng")
    config = MngConfig(default_host_dir=tmp_path / "host")
    mng_ctx = MngContext.model_construct(
        config=config,
        pm=pm,
        profile_dir=tmp_path / "profile",
    )

    host = cast(OnlineHostInterface, FakeHost(is_local=True, host_dir=tmp_path / "host"))

    agent = ElenaCodeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-elena"),
        agent_type=AgentTypeName("elena-code"),
        work_dir=tmp_path / "work",
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mng_ctx=mng_ctx,
        agent_config=ClaudeZygoteConfig(check_installation=False),
        host=host,
    )
    return agent, host


def test_elena_code_registers_with_claude_zygote_config() -> None:
    """Verify that register_agent_type returns ClaudeZygoteConfig (not ClaudeAgentConfig).

    This ensures elena-code inherits trust_working_directory=True so the
    Claude trust dialog does not appear when deploying with --in-place.
    """
    _agent_type_name, _agent_class, config_class = register_agent_type()
    assert config_class is ClaudeZygoteConfig


def test_elena_code_agent_inherits_from_claude_zygote_agent() -> None:
    """Verify that ElenaCodeAgent is a subclass of ClaudeZygoteAgent."""
    assert issubclass(ElenaCodeAgent, ClaudeZygoteAgent)


def test_elena_code_agent_inherits_from_claude_agent() -> None:
    """Verify that ElenaCodeAgent is transitively a subclass of ClaudeAgent."""
    assert issubclass(ElenaCodeAgent, ClaudeAgent)


def test_adds_agent_ttyd_for_elena_code_type() -> None:
    """Verify that the plugin adds an agent ttyd command for elena-code agents."""
    params: dict[str, Any] = {"add_command": (), "agent_type": "elena-code"}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert len(params["add_command"]) == 1
    assert AGENT_TTYD_WINDOW_NAME in params["add_command"][0]
    assert AGENT_TTYD_COMMAND in params["add_command"][0]


def test_does_not_modify_non_create_commands() -> None:
    """Verify that the plugin does not modify params for non-create commands."""
    params: dict[str, Any] = {"add_command": (), "agent_type": "elena-code"}

    override_command_options(
        command_name="connect",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert params["add_command"] == ()


def test_does_not_modify_for_other_agent_types() -> None:
    """Verify that the plugin does not modify params for non-elena-code agents."""
    params: dict[str, Any] = {"add_command": (), "agent_type": "claude"}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert params["add_command"] == ()


def test_elena_system_prompt_is_conversational() -> None:
    """Verify that the system prompt instructs Elena to be conversational."""
    assert "conversational" in ELENA_SYSTEM_PROMPT.lower()


def test_elena_system_prompt_forbids_code_writing() -> None:
    """Verify that the system prompt instructs Elena not to write code."""
    assert "NEVER write code" in ELENA_SYSTEM_PROMPT


def test_elena_assemble_command_includes_system_prompt(tmp_path: Path) -> None:
    """Verify that ElenaCodeAgent.assemble_command injects the system prompt."""
    agent, host = _make_elena_agent(tmp_path)

    command = agent.assemble_command(host=host, agent_args=(), command_override=None)

    assert "--append-system-prompt" in str(command)


def test_elena_assemble_command_prompt_is_quoted(tmp_path: Path) -> None:
    """Verify that the system prompt is shell-quoted in the assembled command."""
    agent, host = _make_elena_agent(tmp_path)

    command = agent.assemble_command(host=host, agent_args=(), command_override=None)

    # shlex.quote wraps multi-word strings in single quotes
    assert "'" in str(command)
    # The prompt text should appear (quoted) in the command
    assert "Elena" in str(command)


def test_elena_assemble_command_preserves_agent_args(tmp_path: Path) -> None:
    """Verify that additional agent_args are preserved alongside the system prompt."""
    agent, host = _make_elena_agent(tmp_path)

    command = agent.assemble_command(host=host, agent_args=("--model", "sonnet"), command_override=None)

    assert "--append-system-prompt" in str(command)
    assert "--model" in str(command)
    assert "sonnet" in str(command)


def test_elena_assemble_command_merges_existing_system_prompt(tmp_path: Path) -> None:
    """Verify that an existing --append-system-prompt is merged with Elena's prompt."""
    agent, host = _make_elena_agent(tmp_path)
    user_prompt = "Be extra creative."
    agent_args = ("--append-system-prompt", shlex.quote(user_prompt), "--model", "sonnet")

    command = agent.assemble_command(host=host, agent_args=agent_args, command_override=None)

    cmd_str = str(command)
    # The parent produces two command variants (resume || create), so the flag
    # appears twice in the full command string -- but only once per variant.
    assert cmd_str.count("--append-system-prompt") == 2
    assert "Elena" in cmd_str
    assert "Be extra creative." in cmd_str
    assert "--model" in cmd_str


# --- Unit tests for _merge_system_prompt_into_args ---


def test_merge_adds_flag_when_absent() -> None:
    """When --append-system-prompt is not in agent_args, it is prepended."""
    result = _merge_system_prompt_into_args("elena prompt", ("--model", "opus"))

    assert result[0] == "--append-system-prompt"
    assert shlex.split(result[1])[0] == "elena prompt"
    assert result[2:] == ("--model", "opus")


def test_merge_combines_prompts_space_separated_form() -> None:
    """When --append-system-prompt VALUE is present, prompts are merged newline-separated."""
    user_prompt = shlex.quote("user instructions")
    args = ("--append-system-prompt", user_prompt, "--model", "opus")

    result = _merge_system_prompt_into_args("elena prompt", args)

    assert result[0] == "--append-system-prompt"
    merged_value = shlex.split(result[1])[0]
    assert merged_value == "elena prompt\nuser instructions"
    assert result[2:] == ("--model", "opus")


def test_merge_combines_prompts_equals_form() -> None:
    """When --append-system-prompt=VALUE is present, prompts are merged newline-separated."""
    user_prompt = shlex.quote("user instructions")
    args = (f"--append-system-prompt={user_prompt}", "--model", "opus")

    result = _merge_system_prompt_into_args("elena prompt", args)

    flag_and_value = result[0]
    assert flag_and_value.startswith("--append-system-prompt=")
    value_part = flag_and_value[len("--append-system-prompt=") :]
    merged_value = shlex.split(value_part)[0]
    assert merged_value == "elena prompt\nuser instructions"
    assert result[1:] == ("--model", "opus")


def test_merge_handles_unquoted_user_value() -> None:
    """When the user value is not shell-quoted, it is still merged correctly."""
    args = ("--append-system-prompt", "simple_value")

    result = _merge_system_prompt_into_args("elena prompt", args)

    merged_value = shlex.split(result[1])[0]
    assert merged_value == "elena prompt\nsimple_value"


def test_merge_only_flag_no_value_treated_as_absent() -> None:
    """When --append-system-prompt is the last token (no value follows), flag is prepended."""
    args = ("--model", "opus", "--append-system-prompt")

    result = _merge_system_prompt_into_args("elena prompt", args)

    assert result[0] == "--append-system-prompt"
    assert shlex.split(result[1])[0] == "elena prompt"
    assert result[2:] == ("--model", "opus", "--append-system-prompt")
