from urwid.widget.wimp import CheckBox

from imbue.mngr.cli.plugin_install_wizard import _filter_already_installed
from imbue.mngr.cli.plugin_install_wizard import _get_accepted_signals
from imbue.mngr.cli.plugin_install_wizard import _get_selected_entries
from imbue.mngr.cli.plugin_install_wizard import _is_dependent_visible
from imbue.mngr.cli.plugin_install_wizard import _phase2_dependent_entries
from imbue.mngr.cli.plugin_install_wizard import _should_preselect_basic
from imbue.mngr.plugin_catalog import CatalogEntry
from imbue.mngr.plugin_catalog import RequiredPackagesGate
from imbue.mngr.plugin_catalog import SignalCheck
from imbue.mngr.plugin_catalog import SignalGate
from imbue.mngr.primitives import PluginTier

_PASSING_SIGNAL = SignalCheck(command=("true",))
_FAILING_SIGNAL = SignalCheck(command=("false",))

# Abstract fixtures for the gating-logic tests: an agent plugin, a base plugin, and
# a per-agent extra that is package-gated on both. Names are deliberately fake so
# they aren't mistaken for real catalog entries (the real entries are exercised in
# plugin_catalog_test.py).
_AGENT_PLUGIN = CatalogEntry(
    entry_point_name="agent_x",
    package_name="pkg-agent-x",
    description="Fake agent plugin",
    tier=PluginTier.INDEPENDENT,
    gate=SignalGate(signal=_PASSING_SIGNAL),
    is_recommended=True,
)
_BASE_PLUGIN = CatalogEntry(
    entry_point_name="base_y",
    package_name="pkg-base-y",
    description="Fake base plugin",
    tier=PluginTier.INDEPENDENT,
    is_recommended=True,
)
_AGENT_EXTRA = CatalogEntry(
    entry_point_name="agent_x_extra",
    package_name="pkg-agent-x-extra",
    description="Fake per-agent extra requiring the agent plugin and base plugin",
    tier=PluginTier.DEPENDENT,
    is_recommended=True,
    gate=RequiredPackagesGate(packages=("pkg-agent-x", "pkg-base-y")),
)

# =============================================================================
# Tests for _should_preselect_basic
# =============================================================================


def test_should_preselect_basic_with_passing_signal() -> None:
    """A BASIC entry with a passing signal gate should be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_PASSING_SIGNAL),
    )
    assert _should_preselect_basic(entry) is True


def test_should_preselect_basic_with_failing_signal() -> None:
    """A BASIC entry with a failing signal gate should not be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_FAILING_SIGNAL),
    )
    assert _should_preselect_basic(entry) is False


def test_should_preselect_basic_no_gate() -> None:
    """A BASIC entry with no gate should always be preselected."""
    entry = CatalogEntry(
        entry_point_name="test",
        package_name="test",
        description="test",
        tier=PluginTier.INDEPENDENT,
        gate=None,
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
            entry_point_name="with_signal",
            package_name="pkg-a",
            description="d",
            tier=PluginTier.INDEPENDENT,
            gate=SignalGate(signal=_PASSING_SIGNAL),
        ),
        CatalogEntry(
            entry_point_name="no_gate",
            package_name="pkg-b",
            description="d",
            tier=PluginTier.INDEPENDENT,
        ),
    ]
    accepted = _get_accepted_signals(selected)
    assert _PASSING_SIGNAL in accepted
    assert len(accepted) == 1


def test_get_accepted_signals_empty_when_no_gates() -> None:
    selected = [
        CatalogEntry(
            entry_point_name="no_gate",
            package_name="pkg-a",
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
# Tests for _is_dependent_visible (signal gate vs required-packages gate)
# =============================================================================


def test_is_dependent_visible_package_gated_all_present() -> None:
    present = frozenset({"pkg-agent-x", "pkg-base-y"})
    assert _is_dependent_visible(_AGENT_EXTRA, set(), present) is True


def test_is_dependent_visible_package_gated_one_missing() -> None:
    # Base plugin present but the agent plugin is not -> not offered.
    present = frozenset({"pkg-base-y"})
    assert _is_dependent_visible(_AGENT_EXTRA, set(), present) is False


def test_is_dependent_visible_package_gated_ignores_signals() -> None:
    """A required-packages gate is decided purely by package presence, not signals."""
    assert _is_dependent_visible(_AGENT_EXTRA, {_PASSING_SIGNAL}, frozenset()) is False


def test_is_dependent_visible_signal_gated() -> None:
    """A signal gate is unlocked only when its signal was accepted in phase 1."""
    signal_gated = CatalogEntry(
        entry_point_name="signal_extra",
        package_name="pkg-signal-extra",
        description="signal-gated dependent",
        tier=PluginTier.DEPENDENT,
        gate=SignalGate(signal=_PASSING_SIGNAL),
    )
    assert _is_dependent_visible(signal_gated, {_PASSING_SIGNAL}, frozenset()) is True
    assert _is_dependent_visible(signal_gated, set(), frozenset()) is False


def test_is_dependent_visible_no_gate_never_unlocked() -> None:
    """A dependent with no gate is never offered."""
    no_gate = CatalogEntry(
        entry_point_name="orphan_extra",
        package_name="pkg-orphan-extra",
        description="dependent with no gate",
        tier=PluginTier.DEPENDENT,
    )
    assert _is_dependent_visible(no_gate, {_PASSING_SIGNAL}, frozenset({"pkg-orphan-extra"})) is False


# =============================================================================
# Tests for _phase2_dependent_entries ("present" = installed or selected)
# =============================================================================


def test_phase2_extra_offered_when_agent_and_base_both_selected() -> None:
    """Selecting both the agent plugin and base plugin in phase 1 unlocks the extra."""
    result = _phase2_dependent_entries((_AGENT_EXTRA,), [_AGENT_PLUGIN, _BASE_PLUGIN], frozenset())
    assert result == (_AGENT_EXTRA,)


def test_phase2_extra_offered_when_agent_installed_and_base_selected() -> None:
    """The agent plugin already installed (filtered out of phase 1) still counts as present."""
    result = _phase2_dependent_entries((_AGENT_EXTRA,), [_BASE_PLUGIN], frozenset({"pkg-agent-x"}))
    assert result == (_AGENT_EXTRA,)


def test_phase2_extra_not_offered_without_base() -> None:
    """Having the agent plugin but not the base plugin does not unlock the extra."""
    result = _phase2_dependent_entries((_AGENT_EXTRA,), [_AGENT_PLUGIN], frozenset())
    assert result == ()


def test_phase2_extra_not_offered_without_agent() -> None:
    """Having the base plugin but not the agent plugin does not unlock the extra."""
    result = _phase2_dependent_entries((_AGENT_EXTRA,), [_BASE_PLUGIN], frozenset())
    assert result == ()
