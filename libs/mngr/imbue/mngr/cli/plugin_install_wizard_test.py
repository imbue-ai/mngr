from urwid.widget.wimp import CheckBox

from imbue.mngr.cli.plugin_install_wizard import _compute_phase2_plugins
from imbue.mngr.cli.plugin_install_wizard import _dedup_package_names
from imbue.mngr.cli.plugin_install_wizard import _filter_already_installed
from imbue.mngr.cli.plugin_install_wizard import _get_accepted_signals
from imbue.mngr.cli.plugin_install_wizard import _get_selected_entries
from imbue.mngr.cli.plugin_install_wizard import _should_preselect_basic
from imbue.mngr.plugin_catalog import CatalogEntry
from imbue.mngr.plugin_catalog import ClaudeSignalCheck
from imbue.mngr.plugin_catalog import OpenCodeSignalCheck
from imbue.mngr.plugin_catalog import SignalCheck
from imbue.mngr.primitives import PluginTier

_CLAUDE_SIGNAL = ClaudeSignalCheck()
_OPENCODE_SIGNAL = OpenCodeSignalCheck()

# =============================================================================
# Tests for _should_preselect_basic
# =============================================================================


def test_should_preselect_basic_no_signal() -> None:
    """A BASIC-tier entry with no signal is always preselected.

    The signal=None short-circuit is the only logic unique to this helper; the
    signal-present branch is a straight delegation to ``check_signal``, whose
    pass/fail/missing-binary behavior is covered directly in
    ``plugin_catalog_test.py``. Testing that delegation here would duplicate
    those tests, so we only pin the short-circuit.
    """
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


# =============================================================================
# Tests for _compute_phase2_plugins (phase-2 signal gating)
# =============================================================================


def _independent(entry_point_name: str, *, signal: SignalCheck | None = None) -> CatalogEntry:
    return CatalogEntry(
        entry_point_name=entry_point_name,
        package_name=entry_point_name,
        description=entry_point_name,
        tier=PluginTier.INDEPENDENT,
        signal=signal,
    )


def _dependent(entry_point_name: str, *, signal: SignalCheck, package_name: str | None = None) -> CatalogEntry:
    return CatalogEntry(
        entry_point_name=entry_point_name,
        package_name=package_name or entry_point_name,
        description=entry_point_name,
        tier=PluginTier.DEPENDENT,
        signal=signal,
    )


def test_compute_phase2_plugins_includes_dependent_only_for_accepted_signal() -> None:
    """A DEPENDENT plugin appears only when its signal was accepted in phase 1."""
    rest_independent = (_independent("extra"),)
    dependent = (
        _dependent("guardian", signal=_CLAUDE_SIGNAL),
        _dependent("oc_extra", signal=_OPENCODE_SIGNAL),
    )
    # Only the claude signal was accepted.
    result = _compute_phase2_plugins(rest_independent, dependent, {_CLAUDE_SIGNAL})

    names = [e.entry_point_name for e in result]
    # Non-recommended independents always appear; the claude-gated dependent
    # appears; the opencode-gated dependent is filtered out.
    assert names == ["extra", "guardian"]


def test_compute_phase2_plugins_excludes_all_dependents_when_no_signal_accepted() -> None:
    """With no accepted signals, every DEPENDENT plugin is gated out."""
    rest_independent = (_independent("extra-a"), _independent("extra-b"))
    dependent = (_dependent("guardian", signal=_CLAUDE_SIGNAL),)

    result = _compute_phase2_plugins(rest_independent, dependent, set())

    assert [e.entry_point_name for e in result] == ["extra-a", "extra-b"]


def test_compute_phase2_plugins_includes_multiple_dependents_for_same_signal() -> None:
    """All DEPENDENT plugins sharing an accepted signal are surfaced."""
    dependent = (
        _dependent("guardian", signal=_CLAUDE_SIGNAL),
        _dependent("fixme", signal=_CLAUDE_SIGNAL),
    )

    result = _compute_phase2_plugins((), dependent, {_CLAUDE_SIGNAL})

    assert [e.entry_point_name for e in result] == ["guardian", "fixme"]


# =============================================================================
# Tests for _dedup_package_names (package dedup across entry points)
# =============================================================================


def test_dedup_package_names_collapses_shared_package() -> None:
    """Entry points that share a package collapse to a single package name."""
    entries = [
        _dependent("claude", signal=_CLAUDE_SIGNAL, package_name="imbue-mngr-claude"),
        _dependent("code_guardian", signal=_CLAUDE_SIGNAL, package_name="imbue-mngr-claude"),
        _independent("pair"),
    ]
    # 'pair' is independent with package_name == entry_point_name == "pair".
    assert _dedup_package_names(entries) == ["imbue-mngr-claude", "pair"]


def test_dedup_package_names_preserves_first_seen_order() -> None:
    """De-duplication keeps the order in which packages were first seen."""
    entries = [
        _independent("b"),
        _independent("a"),
        _dependent("a_extra", signal=_CLAUDE_SIGNAL, package_name="a"),
        _independent("c"),
    ]
    assert _dedup_package_names(entries) == ["b", "a", "c"]


def test_dedup_package_names_empty() -> None:
    """An empty selection yields no package names."""
    assert _dedup_package_names([]) == []
