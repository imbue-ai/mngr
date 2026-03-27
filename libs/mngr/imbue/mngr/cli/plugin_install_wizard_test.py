from urwid.widget.wimp import CheckBox

from imbue.mngr.cli.plugin_install_wizard import _filter_already_installed
from imbue.mngr.cli.plugin_install_wizard import _get_selected_package_names
from imbue.mngr.plugin_catalog import RECOMMENDED_PLUGINS
from imbue.mngr.plugin_catalog import RecommendedPlugin

# =============================================================================
# Tests for RECOMMENDED_PLUGINS
# =============================================================================


def test_recommended_plugins_contains_expected_packages() -> None:
    """RECOMMENDED_PLUGINS should include the published mngr-* plugins."""
    names = {p.package_name for p in RECOMMENDED_PLUGINS}
    assert "mngr-opencode" in names
    assert "mngr-pair" in names
    assert "mngr-tutor" in names


def test_recommended_plugins_mngr_tutor_is_preselected() -> None:
    """mngr-tutor should be the only pre-selected plugin."""
    preselected = [p for p in RECOMMENDED_PLUGINS if p.is_preselected]
    assert len(preselected) == 1
    assert preselected[0].package_name == "mngr-tutor"


def test_recommended_plugins_all_have_descriptions() -> None:
    """Every recommended plugin should have a non-empty description."""
    for plugin in RECOMMENDED_PLUGINS:
        assert plugin.description, f"{plugin.package_name} has no description"


# =============================================================================
# Tests for _filter_already_installed
# =============================================================================


def test_filter_already_installed_removes_installed() -> None:
    """_filter_already_installed should remove plugins whose names are in the installed set."""
    plugins = (
        RecommendedPlugin(package_name="a", description="Plugin A"),
        RecommendedPlugin(package_name="b", description="Plugin B"),
        RecommendedPlugin(package_name="c", description="Plugin C"),
    )
    installed = frozenset({"b"})
    result = _filter_already_installed(plugins, installed)
    assert len(result) == 2
    assert result[0].package_name == "a"
    assert result[1].package_name == "c"


def test_filter_already_installed_all_installed() -> None:
    """_filter_already_installed should return empty tuple when all are installed."""
    plugins = (
        RecommendedPlugin(package_name="a", description="A"),
        RecommendedPlugin(package_name="b", description="B"),
    )
    installed = frozenset({"a", "b"})
    result = _filter_already_installed(plugins, installed)
    assert result == ()


def test_filter_already_installed_none_installed() -> None:
    """_filter_already_installed should return all plugins when none are installed."""
    plugins = (
        RecommendedPlugin(package_name="a", description="A"),
        RecommendedPlugin(package_name="b", description="B"),
    )
    result = _filter_already_installed(plugins, frozenset())
    assert result == plugins


# =============================================================================
# Tests for _get_selected_package_names
# =============================================================================


def test_get_selected_package_names_returns_checked() -> None:
    """_get_selected_package_names should return names of checked plugins."""
    plugins = (
        RecommendedPlugin(package_name="a", description="A"),
        RecommendedPlugin(package_name="b", description="B"),
        RecommendedPlugin(package_name="c", description="C"),
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
    plugins = (RecommendedPlugin(package_name="a", description="A"),)
    checkboxes = [CheckBox("a", state=False)]
    result = _get_selected_package_names(plugins, checkboxes)
    assert result == []


def test_get_selected_package_names_all_checked() -> None:
    """_get_selected_package_names should return all names when everything is checked."""
    plugins = (
        RecommendedPlugin(package_name="a", description="A"),
        RecommendedPlugin(package_name="b", description="B"),
    )
    checkboxes = [
        CheckBox("a", state=True),
        CheckBox("b", state=True),
    ]
    result = _get_selected_package_names(plugins, checkboxes)
    assert result == ["a", "b"]
