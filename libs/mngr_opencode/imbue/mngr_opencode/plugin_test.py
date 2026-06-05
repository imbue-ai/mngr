"""Unit tests for OpenCodeAgentConfig and the opencode plugin hook."""

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.primitives import CommandString
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig
from imbue.mngr_opencode.plugin import register_agent_type


def test_opencode_agent_config_command_default_is_opencode() -> None:
    """The plugin-specific default ``command`` is what makes ``mngr create <name> opencode`` run ``opencode``.

    Only the plugin-added field (``command``) plus the two inherited scalars
    that the README documents are pinned here; the inherited aggregate-field
    defaults (``env``, ``extra_provision_command``, etc.) are covered by the
    base ``AgentTypeConfig`` tests.
    """
    config = OpenCodeAgentConfig()

    assert str(config.command) == "opencode"
    assert config.cli_args == ()
    assert config.parent_type is None


def test_merge_with_replaces_cli_args() -> None:
    """Override cli_args replace (not concatenate onto) the base, per assign-by-default merge semantics.

    A non-empty base is required to distinguish replacement from the previous
    buggy concatenation: with an empty base both yield the same result.
    """
    base = OpenCodeAgentConfig(cli_args=("--base",))
    override = OpenCodeAgentConfig(cli_args=("--override",))

    merged = base.merge_with(override)

    assert isinstance(merged, OpenCodeAgentConfig)
    assert merged.cli_args == ("--override",)
    # The command default is untouched when the override does not set it.
    assert str(merged.command) == "opencode"


def test_merge_with_preserves_base_fields_absent_from_override() -> None:
    """Fields set on the base but not on the override survive the merge.

    Guards against the prior bug where the custom merge_with dropped every
    field except parent_type/cli_args/command, silently discarding a user's
    env / extra_provision_command settings.
    """
    base = OpenCodeAgentConfig(env=("FOO=bar",), extra_provision_command=("echo hi",))
    override = OpenCodeAgentConfig(cli_args=("--x",))

    merged = base.merge_with(override)

    assert merged.env == ("FOO=bar",)
    assert merged.extra_provision_command == ("echo hi",)
    assert merged.cli_args == ("--x",)


def test_merge_with_override_command_wins() -> None:
    """An explicitly-set command on the override replaces the base command."""
    base = OpenCodeAgentConfig()
    override = OpenCodeAgentConfig(command=CommandString("opencode-custom"))

    merged = base.merge_with(override)

    assert str(merged.command) == "opencode-custom"


def test_register_agent_type_returns_opencode_class_and_config() -> None:
    """The plugin hook registers the opencode type backed by BaseAgent and OpenCodeAgentConfig."""
    name, agent_class, config_class = register_agent_type()

    assert name == "opencode"
    assert agent_class is BaseAgent
    assert config_class is OpenCodeAgentConfig
