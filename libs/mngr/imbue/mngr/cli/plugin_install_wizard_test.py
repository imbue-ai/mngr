from unittest.mock import patch

from urwid.widget.wimp import CheckBox

from imbue.mngr.cli.plugin_install_wizard import _filter_already_installed
from imbue.mngr.cli.plugin_install_wizard import _get_selected_package_names
from imbue.mngr.cli.plugin_install_wizard import _should_preselect
from imbue.mngr.plugin_catalog import CatalogEntry
from imbue.mngr.plugin_catalog import PLUGIN_CATALOG
from imbue.mngr.plugin_catalog import SignalCheck
from imbue.mngr.primitives import PluginTier

# =============================================================================
# Tests for PLUGIN_CATALOG
# =============================================================================


def test_catalog_contains_expected_basic_entry_points() -> None:
    """PLUGIN_CATALOG should include the main agent-type plugins as BASIC tier."""
    basic_names = {e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.BASIC}
    assert "claude" in basic_names
    assert "opencode" in basic_names
    assert "llm" in basic_names


def test_catalog_basic_entries_have_signals() -> None:
    """Every BASIC-tier entry should have a signal key."""
    for entry in PLUGIN_CATALOG:
        if entry.tier == PluginTier.BASIC:
            assert entry.signal is not None, f"BASIC entry {entry.entry_point_name} has no signal"


def test_catalog_all_have_descriptions() -> None:
    """Every catalog entry should have a non-empty description."""
    for entry in PLUGIN_CATALOG:
        assert entry.description, f"{entry.entry_point_name} has no description"


# =============================================================================
# Tests for _should_preselect
# =============================================================================


def test_should_preselect_basic_with_passing_signal() -> None:
    """BASIC tier with passing signal should be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.BASIC,
        signal="test_signal",
    )
    test_signal = SignalCheck(command=("true",))
    with patch("imbue.mngr.cli.plugin_install_wizard.SIGNAL_CHECKS", {"test_signal": test_signal}):
        assert _should_preselect(entry) is True


def test_should_preselect_basic_with_failing_signal() -> None:
    """BASIC tier with failing signal should not be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.BASIC,
        signal="test_signal",
    )
    test_signal = SignalCheck(command=("false",))
    with patch("imbue.mngr.cli.plugin_install_wizard.SIGNAL_CHECKS", {"test_signal": test_signal}):
        assert _should_preselect(entry) is False


def test_should_preselect_extra_tier_is_never_preselected() -> None:
    """EXTRA tier should never be preselected even with a signal."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.EXTRA,
        signal="claude",
    )
    assert _should_preselect(entry) is False


def test_should_preselect_basic_no_signal() -> None:
    """BASIC tier with no signal should not be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.BASIC,
        signal=None,
    )
    assert _should_preselect(entry) is False


def test_should_preselect_basic_unknown_signal() -> None:
    """BASIC tier with unknown signal key should not be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.BASIC,
        signal="nonexistent_signal_key",
    )
    assert _should_preselect(entry) is False


# =============================================================================
# Tests for _filter_already_installed
# =============================================================================


def test_filter_already_installed_removes_installed() -> None:
    """_filter_already_installed should remove plugins whose names are in the installed set."""
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="Plugin A", tier=PluginTier.EXTRA),
        CatalogEntry(entry_point_name="b", package_name="b", description="Plugin B", tier=PluginTier.EXTRA),
        CatalogEntry(entry_point_name="c", package_name="c", description="Plugin C", tier=PluginTier.EXTRA),
    )
    installed = frozenset({"b"})
    result = _filter_already_installed(plugins, installed)
    assert len(result) == 2
    assert result[0].package_name == "a"
    assert result[1].package_name == "c"


def test_filter_already_installed_all_installed() -> None:
    """_filter_already_installed should return empty tuple when all are installed."""
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.EXTRA),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.EXTRA),
    )
    installed = frozenset({"a", "b"})
    result = _filter_already_installed(plugins, installed)
    assert result == ()


def test_filter_already_installed_none_installed() -> None:
    """_filter_already_installed should return all plugins when none are installed."""
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.EXTRA),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.EXTRA),
    )
    result = _filter_already_installed(plugins, frozenset())
    assert result == plugins


# =============================================================================
# Tests for _get_selected_package_names
# =============================================================================


def test_get_selected_package_names_returns_checked() -> None:
    """_get_selected_package_names should return names of checked plugins."""
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.EXTRA),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.EXTRA),
        CatalogEntry(entry_point_name="c", package_name="c", description="C", tier=PluginTier.EXTRA),
    )
    checkboxes = [
        CheckBox("a", state=True),
        CheckBox("b", state=False),
        CheckBox("c", state=True),
    ]
    result = _get_selected_package_names(plugins, checkboxes)
    assert result == ["a", "c"]


def test_get_selected_package_names_none_checked() -> None:
    """_get_selected_package_names should return empty list when nothing is checked."""
    plugins = (CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.EXTRA),)
    checkboxes = [CheckBox("a", state=False)]
    result = _get_selected_package_names(plugins, checkboxes)
    assert result == []


def test_get_selected_package_names_all_checked() -> None:
    """_get_selected_package_names should return all names when everything is checked."""
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.EXTRA),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.EXTRA),
    )
    checkboxes = [
        CheckBox("a", state=True),
        CheckBox("b", state=True),
    ]
    result = _get_selected_package_names(plugins, checkboxes)
    assert result == ["a", "b"]
