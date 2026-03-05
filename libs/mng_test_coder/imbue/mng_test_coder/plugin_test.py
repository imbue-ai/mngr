"""Unit tests for the test-coder agent type plugin."""

from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng_test_coder.plugin import TestCoderAgent
from imbue.mng_test_coder.plugin import TestCoderConfig
from imbue.mng_test_coder.plugin import register_agent_type


def test_register_agent_type_returns_correct_tuple() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "test-coder"
    assert agent_class is TestCoderAgent
    assert config_class is TestCoderConfig


def test_test_coder_config_defaults() -> None:
    config = TestCoderConfig()
    assert config.install_llm_echo is True
    assert config.install_llm is True
    assert config.trust_working_directory is True


def test_test_coder_config_is_agent_type_config() -> None:
    config = TestCoderConfig()
    assert isinstance(config, AgentTypeConfig)


def test_test_coder_agent_is_agent_interface_subclass() -> None:
    assert issubclass(TestCoderAgent, AgentInterface)
