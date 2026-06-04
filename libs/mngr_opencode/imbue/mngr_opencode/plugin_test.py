"""Unit tests for OpenCodeAgentConfig."""

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig


def test_opencode_agent_config_has_correct_defaults() -> None:
    """Verify that OpenCodeAgentConfig has the expected default values."""
    config = OpenCodeAgentConfig()

    assert str(config.command) == "opencode"
    assert config.cli_args == ()
    assert config.parent_type is None


def test_opencode_agent_config_merge_with_override() -> None:
    """Verify that merge_with works correctly for OpenCodeAgentConfig."""
    base = OpenCodeAgentConfig()
    override = OpenCodeAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, OpenCodeAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "opencode"


def test_opencode_agent_config_merge_preserves_unset_base_fields() -> None:
    """An override that does not set a field must not clear it from the base.

    OpenCodeAgentConfig relies on AgentTypeConfig.merge_with, which keys off
    model_fields_set. A field present on the base (here, ``env``) and absent
    from the override must survive the merge rather than reset to its default.
    """
    base = OpenCodeAgentConfig(env=("FOO=bar",), extra_provision_command=("setup.sh",))
    override = OpenCodeAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, OpenCodeAgentConfig)
    assert merged.env == ("FOO=bar",)
    assert merged.extra_provision_command == ("setup.sh",)
    assert merged.cli_args == ("--verbose",)


def test_opencode_agent_config_merge_cli_args_is_assign_by_default() -> None:
    """cli_args follows the framework-wide assign-by-default merge contract.

    An override that sets cli_args replaces the base value entirely (additive
    behavior requires the ``cli_args__extend`` operator), so the merged result
    must equal the override's value, not the concatenation of both.
    """
    base = OpenCodeAgentConfig(cli_args=("--from-base",))
    override = OpenCodeAgentConfig(cli_args=("--from-override",))

    merged = base.merge_with(override)

    assert merged.cli_args == ("--from-override",)


def test_opencode_agent_config_merge_accepts_base_class_override() -> None:
    """Merging a plain AgentTypeConfig override into an OpenCodeAgentConfig base must not raise.

    A secondary config file that redefines the same custom type without
    repeating ``parent_type`` is parsed as the base ``AgentTypeConfig``. The
    inherited ``merge_with`` permits this (its check is
    ``isinstance(self, type(override))``), so the merge must succeed, preserve
    the OpenCodeAgentConfig type, and apply the override's value.
    """
    base = OpenCodeAgentConfig()
    override = AgentTypeConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, OpenCodeAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "opencode"
