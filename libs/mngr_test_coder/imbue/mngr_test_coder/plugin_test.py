"""Unit tests for the test-coder agent type plugin."""

from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr_mind.conftest import StubCommandResult
from imbue.mngr_mind.conftest import StubHost
from imbue.mngr_test_coder.plugin import TestCoderAgent
from imbue.mngr_test_coder.plugin import TestCoderConfig
from imbue.mngr_test_coder.plugin import _LLM_MATCHED_RESPONSES_LOCAL_CHECKOUT
from imbue.mngr_test_coder.plugin import _configure_model_as_default
from imbue.mngr_test_coder.plugin import _install_llm_matched_responses_plugin
from imbue.mngr_test_coder.plugin import register_agent_type


def test_register_agent_type_returns_correct_tuple() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "test-coder"
    assert agent_class is TestCoderAgent
    assert config_class is TestCoderConfig


def test_test_coder_config_defaults() -> None:
    config = TestCoderConfig()
    assert config.install_llm_matched_responses is True
    assert config.install_llm is True
    assert config.trust_working_directory is True


# --- assemble_command tests ---


def test_assemble_command_returns_idle_loop() -> None:
    """assemble_command should return an idle loop command."""
    agent = TestCoderAgent.__new__(TestCoderAgent)
    host = StubHost()
    result = agent.assemble_command(cast(Any, host), (), None)
    assert isinstance(result, CommandString)
    assert "sleep 60" in str(result)
    assert "Test agent running" in str(result)


# --- _configure_model_as_default tests ---


def test_configure_model_as_default_writes_toml() -> None:
    """_configure_model_as_default should write minds.toml with matched-responses."""
    stub = StubHost()
    work_dir = Path("/work")
    _configure_model_as_default(cast(OnlineHostInterface, stub), work_dir)
    assert len(stub.written_text_files) == 1
    path, content = stub.written_text_files[0]
    assert path == work_dir / "minds.toml"
    assert "matched-responses" in content


# --- _install_llm_matched_responses_plugin tests ---


def test_install_llm_matched_responses_succeeds_from_pypi() -> None:
    """Plugin install from PyPI should succeed when the command succeeds."""
    host = StubHost(
        command_results={"llm install": StubCommandResult(success=True)},
    )
    _install_llm_matched_responses_plugin(cast(OnlineHostInterface, host))


@pytest.mark.skipif(
    not _LLM_MATCHED_RESPONSES_LOCAL_CHECKOUT.exists(),
    reason="llm-matched-responses local checkout not available",
)
def test_install_llm_matched_responses_falls_back_to_local() -> None:
    """Should fall back to local checkout when PyPI fails and local checkout exists."""
    host = StubHost(
        command_results={
            "llm install llm-matched-responses": StubCommandResult(success=False, stderr="not found"),
            "llm install -e": StubCommandResult(success=True),
        },
    )
    _install_llm_matched_responses_plugin(cast(OnlineHostInterface, host))
