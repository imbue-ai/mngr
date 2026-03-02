"""Tests for agent_config_registry and agent_class_registry modules."""

import pytest

from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.agent_class_registry import reset_agent_class_registry
from imbue.mng.config.agent_config_registry import _apply_custom_overrides_to_parent_config
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.errors import MngError


def test_apply_custom_overrides_returns_parent_when_no_overrides() -> None:
    """_apply_custom_overrides_to_parent_config should return parent unchanged when custom has no overrides."""
    parent = AgentTypeConfig(cli_args=("--model", "opus"))
    custom = AgentTypeConfig()

    result = _apply_custom_overrides_to_parent_config(parent, custom)

    assert result is parent
    assert result.cli_args == ("--model", "opus")


def test_get_agent_class_raises_when_unknown_and_no_default() -> None:
    """get_agent_class should raise MngError when agent type is unknown and no default is set."""
    reset_agent_class_registry()
    with pytest.raises(MngError, match="Unknown agent type 'nonexistent'"):
        get_agent_class("nonexistent")
