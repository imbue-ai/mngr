"""Unit tests for the test-coder agent type plugin."""

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
    assert config.install_llm_matched_responses is True
    assert config.install_llm is True
    assert config.trust_working_directory is True
