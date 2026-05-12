"""Unit tests for :mod:`imbue.mngr_latchkey.config`.

Covers the precedence semantics of :meth:`LatchkeyPluginConfig.merge_with`:
``None`` on the override side means "not set in TOML", so the base
value must be preserved; anything non-``None`` must win. The pattern
matches every other ``mngr_*`` plugin's plugin-config merge.
"""

from pathlib import Path

import pytest

from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr_latchkey.config import LatchkeyPluginConfig


def test_default_values_are_none() -> None:
    """Unset directory / binary keep ``None`` so the CLI fallback chain can see them."""
    config = LatchkeyPluginConfig()
    assert config.directory is None
    assert config.latchkey_binary is None
    assert config.enabled is True


def test_merge_override_wins_when_set() -> None:
    """A populated override replaces the base value for every field it sets."""
    base = LatchkeyPluginConfig(directory=Path("/base/dir"), latchkey_binary="/base/bin")
    override = LatchkeyPluginConfig(
        directory=Path("/override/dir"),
        latchkey_binary="/override/bin",
    )
    merged = base.merge_with(override)
    assert merged.directory == Path("/override/dir")
    assert merged.latchkey_binary == "/override/bin"
    assert merged.enabled is True


def test_merge_override_none_keeps_base() -> None:
    """``None`` on the override side means "not set"; base values survive."""
    base = LatchkeyPluginConfig(directory=Path("/base/dir"), latchkey_binary="/base/bin")
    override = LatchkeyPluginConfig()
    merged = base.merge_with(override)
    assert merged.directory == Path("/base/dir")
    assert merged.latchkey_binary == "/base/bin"


def test_merge_with_base_plugin_config_only_carries_enabled() -> None:
    """Merging with a plain :class:`PluginConfig` keeps every type-specific field on the base.

    This is the path the loader takes when ``[plugins.latchkey]`` has no
    fields beyond ``enabled``; the merged result must keep the
    type-specific fields from the base instance so the CLI's fallback
    chain still sees them.
    """
    base = LatchkeyPluginConfig(directory=Path("/base/dir"), latchkey_binary="/base/bin")
    override = PluginConfig(enabled=False)
    merged = base.merge_with(override)
    assert isinstance(merged, LatchkeyPluginConfig)
    assert merged.directory == Path("/base/dir")
    assert merged.latchkey_binary == "/base/bin"
    assert merged.enabled is False


@pytest.mark.parametrize(
    ("base_binary", "override_binary", "expected"),
    [
        (None, None, None),
        ("/a", None, "/a"),
        (None, "/b", "/b"),
        ("/a", "/b", "/b"),
    ],
)
def test_merge_binary_precedence(
    base_binary: str | None,
    override_binary: str | None,
    expected: str | None,
) -> None:
    base = LatchkeyPluginConfig(latchkey_binary=base_binary)
    override = LatchkeyPluginConfig(latchkey_binary=override_binary)
    merged = base.merge_with(override)
    assert merged.latchkey_binary == expected
