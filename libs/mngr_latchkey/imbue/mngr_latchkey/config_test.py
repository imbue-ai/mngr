"""Unit tests for :mod:`imbue.mngr_latchkey.config`."""

from imbue.mngr_latchkey.config import LatchkeyPluginConfig


def test_default_values_are_none() -> None:
    """Unset directory / binary keep ``None`` so the CLI fallback chain can see them."""
    config = LatchkeyPluginConfig()
    assert config.directory is None
    assert config.latchkey_binary is None
    assert config.enabled is True
