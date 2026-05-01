"""Unit tests for ``SubagentProxyPluginConfig``."""

from __future__ import annotations

from imbue.mngr_subagent_proxy.data_types import SubagentProxyMode
from imbue.mngr_subagent_proxy.data_types import SubagentProxyPluginConfig


def test_default_mode_is_proxy() -> None:
    """Default-constructed config selects PROXY mode (the original behavior).

    Plugin loading must not flip behavior for users who haven't opted in.
    """
    config = SubagentProxyPluginConfig()
    assert config.mode == SubagentProxyMode.PROXY


def test_mode_is_uppercase_string_serializable() -> None:
    """Mode values are uppercase strings: matches UpperCaseStrEnum convention.

    User-facing TOML config uses uppercase values (PROXY, DENY); pinning
    that here so the config doesn't accidentally start accepting
    lowercase.
    """
    assert str(SubagentProxyMode.PROXY) == "PROXY"
    assert str(SubagentProxyMode.DENY) == "DENY"


def test_merge_with_override_mode_wins() -> None:
    """An override config's mode replaces the base's mode on merge."""
    base = SubagentProxyPluginConfig(mode=SubagentProxyMode.PROXY)
    override = SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY)
    merged = base.merge_with(override)
    assert merged.mode == SubagentProxyMode.DENY


def test_merge_with_preserves_enabled_field() -> None:
    """The ``enabled`` field from PluginConfig still merges correctly."""
    base = SubagentProxyPluginConfig(enabled=True, mode=SubagentProxyMode.PROXY)
    override = SubagentProxyPluginConfig(enabled=False, mode=SubagentProxyMode.PROXY)
    merged = base.merge_with(override)
    assert merged.enabled is False
    assert merged.mode == SubagentProxyMode.PROXY
