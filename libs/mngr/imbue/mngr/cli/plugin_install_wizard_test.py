from urwid.widget.wimp import CheckBox

from imbue.mngr.cli.plugin_install_wizard import _filter_already_installed
from imbue.mngr.cli.plugin_install_wizard import _get_accepted_signals
from imbue.mngr.cli.plugin_install_wizard import _get_selected_entries
from imbue.mngr.cli.plugin_install_wizard import _should_preselect_basic
from imbue.mngr.plugin_catalog import CatalogEntry
from imbue.mngr.plugin_catalog import ClaudeSignalCheck
from imbue.mngr.plugin_catalog import SignalCheck
from imbue.mngr.primitives import PluginTier

_PASSING_SIGNAL = SignalCheck(command=("true",))
_FAILING_SIGNAL = SignalCheck(command=("false",))
_CLAUDE_SIGNAL = ClaudeSignalCheck()

# =============================================================================
# Tests for _should_preselect_basic
# =============================================================================


def test_should_preselect_basic_with_passing_signal() -> None:
    """BASIC tier with passing signal should be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.INDEPENDENT,
        signal=_PASSING_SIGNAL,
    )
    assert _should_preselect_basic(entry) is True


def test_should_preselect_basic_with_failing_signal() -> None:
    """BASIC tier with failing signal should not be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.INDEPENDENT,
        signal=_FAILING_SIGNAL,
    )
    assert _should_preselect_basic(entry) is False


def test_should_preselect_basic_no_signal() -> None:
    """BASIC tier with no signal should always be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.INDEPENDENT,
        signal=None,
    )
    assert _should_preselect_basic(entry) is True


# =============================================================================
# Tests for _get_selected_entries
# =============================================================================


def test_get_selected_entries_returns_checked() -> None:
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.DEPENDENT),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.DEPENDENT),
        CatalogEntry(entry_point_name="c", package_name="c", description="C", tier=PluginTier.DEPENDENT),
    )
    checkboxes = [
        CheckBox("a", state=True),
        CheckBox("b", state=False),
        CheckBox("c", state=True),
    ]
    result = _get_selected_entries(plugins, checkboxes)
    assert [e.entry_point_name for e in result] == ["a", "c"]


def test_get_selected_entries_none_checked() -> None:
    plugins = (CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.DEPENDENT),)
    checkboxes = [CheckBox("a", state=False)]
    assert _get_selected_entries(plugins, checkboxes) == []


def test_get_selected_entries_all_checked() -> None:
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.DEPENDENT),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.DEPENDENT),
    )
    checkboxes = [CheckBox("a", state=True), CheckBox("b", state=True)]
    result = _get_selected_entries(plugins, checkboxes)
    assert [e.entry_point_name for e in result] == ["a", "b"]


# =============================================================================
# Tests for _get_accepted_signals
# =============================================================================


def test_get_accepted_signals_returns_signals_from_selected() -> None:
    selected = [
        CatalogEntry(
            entry_point_name="claude",
            package_name="p",
            description="d",
            tier=PluginTier.INDEPENDENT,
            signal=_CLAUDE_SIGNAL,
        ),
        CatalogEntry(
            entry_point_name="tutor",
            package_name="p2",
            description="d",
            tier=PluginTier.INDEPENDENT,
        ),
    ]
    accepted = _get_accepted_signals(selected)
    assert _CLAUDE_SIGNAL in accepted
    assert len(accepted) == 1


def test_get_accepted_signals_empty_when_no_signals() -> None:
    selected = [
        CatalogEntry(
            entry_point_name="tutor",
            package_name="p",
            description="d",
            tier=PluginTier.INDEPENDENT,
        ),
    ]
    assert _get_accepted_signals(selected) == set()


# =============================================================================
# Tests for _filter_already_installed
# =============================================================================


def test_filter_already_installed_removes_installed() -> None:
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="Plugin A", tier=PluginTier.DEPENDENT),
        CatalogEntry(entry_point_name="b", package_name="b", description="Plugin B", tier=PluginTier.DEPENDENT),
        CatalogEntry(entry_point_name="c", package_name="c", description="Plugin C", tier=PluginTier.DEPENDENT),
    )
    installed = frozenset({"b"})
    result = _filter_already_installed(plugins, installed)
    assert len(result) == 2
    assert result[0].package_name == "a"
    assert result[1].package_name == "c"


def test_filter_already_installed_all_installed() -> None:
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.DEPENDENT),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.DEPENDENT),
    )
    installed = frozenset({"a", "b"})
    result = _filter_already_installed(plugins, installed)
    assert result == ()


def test_filter_already_installed_none_installed() -> None:
    plugins = (
        CatalogEntry(entry_point_name="a", package_name="a", description="A", tier=PluginTier.DEPENDENT),
        CatalogEntry(entry_point_name="b", package_name="b", description="B", tier=PluginTier.DEPENDENT),
    )
    result = _filter_already_installed(plugins, frozenset())
    assert result == plugins
