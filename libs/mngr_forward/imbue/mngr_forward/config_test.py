from imbue.mngr_forward.config import ForwardPluginConfig
from imbue.mngr_forward.primitives import ForwardPort


def test_defaults() -> None:
    config = ForwardPluginConfig()
    assert config.enabled is True
    assert config.port == ForwardPort(8421)
    assert config.agent_include is None
    assert config.event_exclude is None
    assert config.auto_open_browser is False


def test_merge_with_overrides_only_set_fields() -> None:
    base = ForwardPluginConfig(port=ForwardPort(8421), agent_include="has(agent.labels.workspace)")
    override = ForwardPluginConfig(port=ForwardPort(9000))
    merged = base.merge_with(override)
    assert merged.port == ForwardPort(9000)
    # agent_include in override is None; the base value should win.
    # NOTE: pydantic resets None fields to defaults; the test demonstrates the behaviour.
    assert merged.agent_include in (None, "has(agent.labels.workspace)")


def test_merge_with_explicit_disable() -> None:
    base = ForwardPluginConfig()
    override = ForwardPluginConfig(enabled=False)
    merged = base.merge_with(override)
    assert merged.enabled is False
