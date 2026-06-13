from imbue.minds.plugin_bundle import installed_mngr_plugin_names
from imbue.minds.plugin_bundle import unbundled_plugin_names
from imbue.mngr.plugin_catalog import get_all_cataloged_entry_point_names


def test_unbundled_is_catalog_minus_installed() -> None:
    installed = installed_mngr_plugin_names()
    unbundled = unbundled_plugin_names()
    assert unbundled == get_all_cataloged_entry_point_names() - installed


def test_unbundled_never_includes_an_installed_plugin() -> None:
    # Disabling a plugin the bundle actually ships would break it; the set we
    # feed into mngr config must be disjoint from what's installed.
    assert unbundled_plugin_names().isdisjoint(installed_mngr_plugin_names())


def test_unbundled_only_contains_cataloged_plugins() -> None:
    assert unbundled_plugin_names() <= get_all_cataloged_entry_point_names()
