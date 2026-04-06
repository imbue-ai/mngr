from imbue.mngr.plugin_catalog import PLUGIN_CATALOG
from imbue.mngr.plugin_catalog import SignalCheck
from imbue.mngr.plugin_catalog import check_signal
from imbue.mngr.plugin_catalog import get_all_cataloged_entry_point_names
from imbue.mngr.plugin_catalog import get_catalog_entry
from imbue.mngr.plugin_catalog import get_installable_packages
from imbue.mngr.primitives import PluginTier

# =============================================================================
# PLUGIN_CATALOG structure
# =============================================================================


def test_catalog_has_entries() -> None:
    assert len(PLUGIN_CATALOG) > 0


def test_catalog_entry_point_names_are_unique() -> None:
    names = [e.entry_point_name for e in PLUGIN_CATALOG]
    assert len(names) == len(set(names))


def test_catalog_signals_are_signal_check_instances() -> None:
    for entry in PLUGIN_CATALOG:
        if entry.signal is not None:
            assert isinstance(entry.signal, SignalCheck), (
                f"Entry {entry.entry_point_name} signal is {type(entry.signal)}, expected SignalCheck"
            )


def test_catalog_contains_expected_basic_entry_points() -> None:
    """PLUGIN_CATALOG should include the main agent-type plugins as BASIC tier."""
    basic_names = {e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.INDEPENDENT}
    assert "claude" in basic_names
    assert "opencode" in basic_names
    assert "llm" in basic_names
    assert "tutor" in basic_names


def test_catalog_entries_sharing_signal_use_same_instance() -> None:
    """Entries that share a signal should reference the exact same object."""
    claude_entry = get_catalog_entry("claude")
    fixme_entry = get_catalog_entry("fixme_fairy")
    assert claude_entry is not None and fixme_entry is not None
    assert claude_entry.signal is fixme_entry.signal


# =============================================================================
# get_catalog_entry
# =============================================================================


def test_get_catalog_entry_found() -> None:
    entry = get_catalog_entry("claude")
    assert entry is not None
    assert entry.entry_point_name == "claude"
    assert entry.tier == PluginTier.INDEPENDENT


def test_get_catalog_entry_not_found() -> None:
    assert get_catalog_entry("nonexistent_plugin_xyz") is None


# =============================================================================
# get_all_cataloged_entry_point_names
# =============================================================================


def test_get_all_cataloged_entry_point_names_matches_catalog() -> None:
    names = get_all_cataloged_entry_point_names()
    expected = {e.entry_point_name for e in PLUGIN_CATALOG}
    assert names == expected


# =============================================================================
# check_signal
# =============================================================================


def test_check_signal_succeeds_for_true_command() -> None:
    signal = SignalCheck(command=("true",))
    assert check_signal(signal) is True


def test_check_signal_fails_for_false_command() -> None:
    signal = SignalCheck(command=("false",))
    assert check_signal(signal) is False


def test_check_signal_fails_for_missing_binary() -> None:
    signal = SignalCheck(command=("nonexistent_binary_xyz_123",))
    assert check_signal(signal) is False


# =============================================================================
# get_installable_packages
# =============================================================================


def test_get_installable_packages_deduplicates_by_package_name() -> None:
    packages = get_installable_packages()
    package_names = [p.package_name for p in packages]
    assert len(package_names) == len(set(package_names))


def test_get_installable_packages_covers_all_packages() -> None:
    packages = get_installable_packages()
    installable_names = {p.package_name for p in packages}
    all_package_names = {e.package_name for e in PLUGIN_CATALOG}
    assert installable_names == all_package_names


def test_get_installable_packages_prefers_basic_tier() -> None:
    """For packages with both BASIC and EXTRA entries, the representative should be BASIC."""
    packages = get_installable_packages()
    for pkg in packages:
        basic_entries = [
            e for e in PLUGIN_CATALOG if e.package_name == pkg.package_name and e.tier == PluginTier.INDEPENDENT
        ]
        if basic_entries:
            assert pkg.tier == PluginTier.INDEPENDENT, (
                f"Package {pkg.package_name} has BASIC entries but representative is {pkg.tier}"
            )
