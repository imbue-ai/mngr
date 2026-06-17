from imbue.mngr.plugin_catalog import PLUGIN_CATALOG
from imbue.mngr.plugin_catalog import RequiredPackagesGate
from imbue.mngr.plugin_catalog import SignalCheck
from imbue.mngr.plugin_catalog import SignalGate
from imbue.mngr.plugin_catalog import UNPUBLISHED_PACKAGES
from imbue.mngr.plugin_catalog import check_signal
from imbue.mngr.plugin_catalog import get_all_cataloged_entry_point_names
from imbue.mngr.plugin_catalog import get_catalog_entry
from imbue.mngr.plugin_catalog import get_installable_packages
from imbue.mngr.primitives import PluginTier

# =============================================================================
# PLUGIN_CATALOG structure
# =============================================================================


def test_catalog_entry_point_names_are_unique() -> None:
    names = [e.entry_point_name for e in PLUGIN_CATALOG]
    assert len(names) == len(set(names))


def test_catalog_contains_expected_basic_entry_points() -> None:
    """PLUGIN_CATALOG should include the main agent-type plugins as BASIC tier."""
    basic_names = {e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.INDEPENDENT}
    assert "claude" in basic_names
    assert "opencode" in basic_names
    assert "tutor" in basic_names


def test_cloud_provider_plugins_detect_their_cli() -> None:
    """aws/gcp/azure are recommended and pre-selected when their CLI is present.

    Each carries a signal that runs the provider's CLI version check, so the
    install wizard pre-selects it when that CLI is on PATH -- the same
    signal-driven recommendation other plugins (claude, modal) use.
    """
    expected_commands = {
        "aws": ("aws", "--version"),
        "gcp": ("gcloud", "--version"),
        "azure": ("az", "--version"),
    }
    for entry_point_name, command in expected_commands.items():
        entry = get_catalog_entry(entry_point_name)
        assert entry is not None
        assert entry.is_recommended is True
        assert isinstance(entry.gate, SignalGate)
        assert entry.gate.signal.command == command


def test_lima_plugin_detects_its_cli() -> None:
    """lima is recommended and pre-selected when limactl is present.

    Its signal only drives wizard preselection because the entry is
    recommended (phase-1); a signal on a non-recommended entry is inert.
    """
    entry = get_catalog_entry("lima")
    assert entry is not None
    assert entry.is_recommended is True
    assert isinstance(entry.gate, SignalGate)
    assert entry.gate.signal.command == ("limactl", "--version")


def test_catalog_entries_sharing_signal_use_same_instance() -> None:
    """Entries that share a signal should reference the exact same SignalCheck object."""
    claude_entry = get_catalog_entry("claude")
    fixme_entry = get_catalog_entry("fixme_fairy")
    assert claude_entry is not None and fixme_entry is not None
    assert isinstance(claude_entry.gate, SignalGate)
    assert isinstance(fixme_entry.gate, SignalGate)
    assert claude_entry.gate.signal is fixme_entry.gate.signal


def test_base_usage_plugin_is_recommended_independent() -> None:
    """The base usage plugin is recommended so it appears (pre-checked) in phase 1."""
    usage = get_catalog_entry("usage")
    assert usage is not None
    assert usage.package_name == "imbue-mngr-usage"
    assert usage.tier == PluginTier.INDEPENDENT
    assert usage.is_recommended is True


def test_agent_usage_providers_require_agent_and_base_usage() -> None:
    """Each per-agent usage provider is DEPENDENT and gated on its agent plugin plus base usage."""
    expected_agent_package = {
        "claude_usage": "imbue-mngr-claude",
        "codex_usage": "imbue-mngr-codex",
        "opencode_usage": "imbue-mngr-opencode",
        "pi_coding_usage": "imbue-mngr-pi-coding",
    }
    for entry_point, agent_package in expected_agent_package.items():
        entry = get_catalog_entry(entry_point)
        assert entry is not None, entry_point
        assert entry.tier == PluginTier.DEPENDENT
        assert isinstance(entry.gate, RequiredPackagesGate), entry_point
        assert set(entry.gate.packages) == {agent_package, "imbue-mngr-usage"}


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


def test_check_signal_fails_for_shell_grep_mismatch() -> None:
    """Exercises the sh -c pipe pattern used by real signals like pi and llm."""
    signal = SignalCheck(command=("sh", "-c", "echo hello | grep -q nonexistent_pattern_xyz"))
    assert check_signal(signal) is False


def test_check_signal_succeeds_for_shell_grep_match() -> None:
    """Exercises the sh -c pipe pattern when the grep matches."""
    signal = SignalCheck(command=("sh", "-c", "echo datasette.io | grep -q datasette.io"))
    assert check_signal(signal) is True


# =============================================================================
# get_installable_packages
# =============================================================================


def test_get_installable_packages_deduplicates_by_package_name() -> None:
    packages = get_installable_packages()
    package_names = [p.package_name for p in packages]
    assert len(package_names) == len(set(package_names))


def test_get_installable_packages_excludes_unpublished() -> None:
    """No package marked unpublished is offered to the install wizard.

    Asserts the exclusion contract against the UNPUBLISHED_PACKAGES constant
    rather than reconstructing the function's output, so it survives catalog
    additions and only fails if the exclusion branch itself regresses.
    """
    installable_names = {p.package_name for p in get_installable_packages()}
    assert installable_names.isdisjoint(UNPUBLISHED_PACKAGES)


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
