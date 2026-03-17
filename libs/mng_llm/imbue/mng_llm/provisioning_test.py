"""Unit tests for provisioning functions."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from imbue.mng_llm.data_types import ProvisioningSettings
from imbue.mng_llm.provisioning import _inject_conversation


def _make_host_stub(
    inject_stdout: str = "Injected message into conversation abc123",
) -> tuple[Any, list[str]]:
    """Create a host stub that records executed commands.

    Returns the host and a list that captures every command string passed to
    execute_command, so callers can assert on the exact shell command that
    ``_inject_conversation`` builds.
    """
    captured_commands: list[str] = []

    def _execute_command(cmd: str, **_kwargs: Any) -> Any:
        captured_commands.append(cmd)
        return SimpleNamespace(success=True, stdout=inject_stdout, stderr="")

    host = SimpleNamespace(execute_command=_execute_command)
    return host, captured_commands


# -- _inject_conversation command construction --


def test_inject_conversation_omits_prompt_flag_when_prompt_is_empty() -> None:
    """Empty prompt must not produce --prompt '' which creates a broken conversation."""
    host, commands = _make_host_stub()
    settings = ProvisioningSettings()

    _inject_conversation(
        host,
        settings,
        model="claude-opus-4.6",
        prompt="",
        response="Hello!",
        label="test",
    )

    assert len(commands) == 1
    assert "--prompt" not in commands[0]
    assert "'Hello!'" in commands[0]


def test_inject_conversation_includes_prompt_flag_when_prompt_is_nonempty() -> None:
    host, commands = _make_host_stub()
    settings = ProvisioningSettings()

    _inject_conversation(
        host,
        settings,
        model="claude-opus-4.6",
        prompt="Start conversation",
        response="Confirmed.",
        label="test",
    )

    assert len(commands) == 1
    assert "--prompt" in commands[0]
    assert "'Start conversation'" in commands[0]


def test_inject_conversation_includes_model_flag() -> None:
    host, commands = _make_host_stub()
    settings = ProvisioningSettings()

    _inject_conversation(
        host,
        settings,
        model="matched-responses",
        prompt="hi",
        response="ok",
        label="test",
    )

    assert "-m 'matched-responses'" in commands[0] or "-m matched-responses" in commands[0]


def test_inject_conversation_sets_env_prefix_for_llm_user_path() -> None:
    host, commands = _make_host_stub()
    settings = ProvisioningSettings()

    _inject_conversation(
        host,
        settings,
        model="test-model",
        prompt="",
        response="ok",
        label="test",
        llm_user_path=Path("/tmp/llm_data"),
    )

    assert commands[0].startswith("LLM_USER_PATH=/tmp/llm_data")


def test_inject_conversation_returns_parsed_conversation_id() -> None:
    host, _ = _make_host_stub(inject_stdout="Injected message into conversation conv-123")
    settings = ProvisioningSettings()

    result = _inject_conversation(
        host,
        settings,
        model="test-model",
        prompt="",
        response="ok",
        label="test",
    )

    assert result == "conv-123"


def _make_failing_host_stub() -> Any:
    """Create a host stub whose execute_command always returns failure."""

    def _execute_command(cmd: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(success=False, stdout="", stderr="error")

    return SimpleNamespace(execute_command=_execute_command)


def test_inject_conversation_returns_none_on_failure() -> None:
    host = _make_failing_host_stub()
    settings = ProvisioningSettings()

    result = _inject_conversation(
        host,
        settings,
        model="test-model",
        prompt="",
        response="ok",
        label="test",
    )

    assert result is None


def test_inject_conversation_includes_extra_env_vars() -> None:
    host, commands = _make_host_stub()
    settings = ProvisioningSettings()

    _inject_conversation(
        host,
        settings,
        model="test-model",
        prompt="hi",
        response="ok",
        label="test",
        env_vars={"LLM_MATCHED_RESPONSE": ""},
    )

    assert "LLM_MATCHED_RESPONSE=''" in commands[0]
