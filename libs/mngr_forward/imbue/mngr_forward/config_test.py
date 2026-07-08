from imbue.mngr_forward.config import ForwardPluginConfig
from imbue.mngr_forward.primitives import ForwardPort


def test_defaults() -> None:
    config = ForwardPluginConfig()
    assert config.enabled is True
    assert config.port == ForwardPort(8421)
    assert config.agent_include is None
    assert config.event_exclude is None
    assert config.auto_open_browser is False
