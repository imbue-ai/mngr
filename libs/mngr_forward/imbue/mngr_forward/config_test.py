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
    # agent_include is not in the override's model_fields_set, so the base value wins.
    assert merged.agent_include == "has(agent.labels.workspace)"


def test_merge_with_explicit_disable() -> None:
    base = ForwardPluginConfig()
    override = ForwardPluginConfig(enabled=False)
    merged = base.merge_with(override)
    assert merged.enabled is False


def test_merge_with_does_not_re_enable_disabled_base() -> None:
    # A base layer that disables the plugin must stay disabled when merged with
    # an override that did not explicitly set ``enabled`` (regression: a
    # hand-written ``value is not None`` merge would resurrect it to True).
    base = ForwardPluginConfig(enabled=False)
    override = ForwardPluginConfig(port=ForwardPort(9000))
    merged = base.merge_with(override)
    assert merged.enabled is False
    assert merged.port == ForwardPort(9000)
