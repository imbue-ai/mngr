"""Interactive plugin install wizard for mngr.

Presents recommended plugins in a two-phase TUI and lets the user select
which ones to install.  Selected plugins are installed in a single
``uv tool install`` invocation.
"""

import sys
from typing import Any
from typing import Final

import click
from loguru import logger
from pydantic import ConfigDict
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.event_loop.main_loop import MainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.divider import Divider
from urwid.widget.frame import Frame
from urwid.widget.listbox import ListBox
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.pile import Pile
from urwid.widget.text import Text
from urwid.widget.wimp import CheckBox

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.pre_readers import find_profile_dir_lightweight
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.plugin_catalog import CatalogEntry
from imbue.mngr.plugin_catalog import PLUGIN_CATALOG
from imbue.mngr.plugin_catalog import SignalCheck
from imbue.mngr.plugin_catalog import check_signal
from imbue.mngr.plugin_catalog import get_installable_packages
from imbue.mngr.primitives import PluginTier
from imbue.mngr.uv_tool import build_uv_tool_install_add_many
from imbue.mngr.uv_tool import read_receipt
from imbue.mngr.uv_tool import require_uv_tool_receipt


class _WizardState(MutableModel):
    """Mutable state for the install wizard TUI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    checkboxes: list[CheckBox]
    plugins: tuple[CatalogEntry, ...]
    is_confirmed: bool = False


class _WizardInputFilter(MutableModel):
    """Input filter that intercepts Enter and q/Ctrl+C before they reach widgets.

    urwid CheckBox handles both Space and Enter by default.  This filter
    captures Enter (to confirm) and q/Ctrl+C (to cancel) so they never
    reach the CheckBox, while letting Space and arrow keys pass through.
    """

    state: _WizardState

    def __call__(self, keys: list[str], raw: list[int]) -> list[str]:
        result: list[str] = []
        for key in keys:
            if key == "enter":
                self.state.is_confirmed = True
                raise ExitMainLoop()
            if key in ("q", "Q", "ctrl c"):
                raise ExitMainLoop()
            result.append(key)
        return result


@pure
def _get_selected_entries(
    plugins: tuple[CatalogEntry, ...],
    checkboxes: list[CheckBox],
) -> list[CatalogEntry]:
    """Return the catalog entries whose checkboxes are checked."""
    return [plugin for plugin, cb in zip(plugins, checkboxes, strict=True) if cb.get_state()]


@pure
def _filter_already_installed(
    plugins: tuple[CatalogEntry, ...],
    installed_names: frozenset[str],
) -> tuple[CatalogEntry, ...]:
    """Remove plugins that are already installed."""
    return tuple(p for p in plugins if p.package_name not in installed_names)


def _should_preselect_basic(entry: CatalogEntry) -> bool:
    """Determine if a BASIC-tier entry should be preselected in phase 1.

    Preselected if there is no signal, or if the signal check passes.
    """
    if entry.signal is None:
        return True
    return check_signal(entry.signal)


def _run_selection_screen(
    plugins: tuple[CatalogEntry, ...],
    preselect: dict[str, bool],
    header_text: str,
) -> list[CatalogEntry] | None:
    """Run a single TUI selection screen.

    Returns the list of selected entries, or None if cancelled.
    ``preselect`` maps entry_point_name to whether that entry should
    start checked.
    """
    if not plugins:
        return []

    name_width = max(len(p.package_name) for p in plugins)

    checkboxes: list[CheckBox] = []
    list_items: list[AttrMap] = []

    for plugin in plugins:
        label = f"{plugin.package_name.ljust(name_width)}  {plugin.description}"
        cb = CheckBox(label, state=preselect.get(plugin.entry_point_name, False))
        checkboxes.append(cb)
        list_items.append(AttrMap(cb, None, focus_map="reversed"))

    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker(list_items)
    listbox = ListBox(list_walker)

    state = _WizardState(checkboxes=checkboxes, plugins=plugins)

    header = Pile(
        [
            AttrMap(Text("Plugin Install Wizard", align="center"), "header"),
            Divider(),
            Text(header_text),
            Divider(),
        ]
    )

    footer = Pile(
        [
            Divider(),
            AttrMap(
                Text("  Space: Toggle | Up/Down: Navigate | Enter: Confirm | q/Ctrl+C: Cancel"),
                "status",
            ),
        ]
    )

    frame = Frame(body=listbox, header=header, footer=footer)

    palette = [
        ("header", "white", "dark blue"),
        ("status", "white", "dark blue"),
        ("reversed", "standout", ""),
    ]

    input_filter = _WizardInputFilter(state=state)

    with create_urwid_screen_preserving_terminal() as screen:
        loop = MainLoop(
            frame,
            palette=palette,
            input_filter=input_filter,
            screen=screen,
        )
        loop.run()

    if not state.is_confirmed:
        return None

    return _get_selected_entries(plugins, checkboxes)


def _get_accepted_signals(selected: list[CatalogEntry]) -> set[SignalCheck]:
    """Return the set of signals whose BASIC plugin was selected."""
    return {entry.signal for entry in selected if entry.signal is not None}


def _run_two_phase_wizard(available: tuple[CatalogEntry, ...]) -> list[str]:
    """Run the two-phase install wizard.

    Phase 1: Recommended INDEPENDENT plugins. Preselected based on signal
             detection (or always if no signal).
    Phase 2: Everything else -- non-recommended INDEPENDENT plugins plus
             DEPENDENT plugins whose signal was accepted in phase 1.
             Preselection based on is_recommended.

    Returns the list of selected package names, or an empty list if cancelled.
    """
    recommended = tuple(e for e in available if e.tier == PluginTier.INDEPENDENT and e.is_recommended)
    rest_independent = tuple(e for e in available if e.tier == PluginTier.INDEPENDENT and not e.is_recommended)
    dependent = tuple(e for e in available if e.tier == PluginTier.DEPENDENT)

    # Phase 1: Recommended plugins
    recommended_preselect = {e.entry_point_name: _should_preselect_basic(e) for e in recommended}

    if recommended:
        phase1_result = _run_selection_screen(
            recommended,
            recommended_preselect,
            "Here are the recommended plugins for mngr.\nDetected tools are pre-selected:",
        )
        if phase1_result is None:
            return []
    else:
        phase1_result = []

    # Determine which signals were accepted in phase 1
    accepted_signals = _get_accepted_signals(phase1_result)

    # Phase 2: non-recommended INDEPENDENT + DEPENDENT (filtered by accepted signals)
    visible_dependent = tuple(e for e in dependent if e.signal in accepted_signals)
    phase2_plugins = rest_independent + visible_dependent

    if phase2_plugins:
        phase2_preselect = {e.entry_point_name: e.is_recommended for e in phase2_plugins}

        phase2_result = _run_selection_screen(
            phase2_plugins,
            phase2_preselect,
            "Do you want to install any extras?",
        )
        if phase2_result is None:
            return []
    else:
        phase2_result = []

    # Combine selections from both phases
    all_selected = phase1_result + phase2_result

    # Deduplicate by package name (multiple entry points can share a package)
    seen: set[str] = set()
    package_names: list[str] = []
    for entry in all_selected:
        if entry.package_name not in seen:
            seen.add(entry.package_name)
            package_names.append(entry.package_name)

    return package_names


def _enable_selected_plugins(selected_package_names: frozenset[str]) -> None:
    """Enable all entry points from the selected packages in user config.

    Uses the same code path as ``mngr plugin enable`` (set_plugin_enabled),
    writing to the user-scope settings file.
    """
    from imbue.mngr.cli.plugin import set_plugin_enabled

    profile_dir = find_profile_dir_lightweight(read_default_host_dir())
    if profile_dir is None:
        logger.warning("Could not find profile directory; skipping plugin enable")
        return

    config_path = get_user_config_path(profile_dir)

    entry_points_to_enable = [e.entry_point_name for e in PLUGIN_CATALOG if e.package_name in selected_package_names]

    for name in entry_points_to_enable:
        set_plugin_enabled(name, is_enabled=True, config_path=config_path)

    if entry_points_to_enable:
        write_human_line("Enabled {} plugin(s) in {}", len(entry_points_to_enable), config_path)


_RELAUNCH_HINT: Final[str] = (
    "You can re-launch the plugin installation wizard with `mngr plugin install-wizard`.\n"
    "See `mngr plugin --help` for more information on plugins."
)


def install_wizard_impl() -> None:
    """Implementation of the install-wizard command.

    Deliberately avoids ``setup_command_context`` / ``MngrContext`` -- all
    we need is the uv-receipt and a ConcurrencyGroup.  Skipping the full
    context setup shaves noticeable time off what is an interactive,
    user-facing flow.
    """
    receipt_path = require_uv_tool_receipt()
    receipt = read_receipt(receipt_path)

    # Filter out already-installed plugins
    installed_names = frozenset(r.name for r in receipt.extras)
    all_packages = get_installable_packages()
    available = _filter_already_installed(all_packages, installed_names)

    if not available:
        write_human_line("All plugins are already installed.")
        return

    # Guard against non-interactive contexts (CI, cron, curl|bash without /dev/tty redirect).
    # urwid requires a real terminal on stdin; without one it raises RuntimeError.
    if not sys.stdin.isatty():
        write_human_line("No interactive terminal detected; skipping plugin install wizard.")
        write_human_line(_RELAUNCH_HINT)
        return

    selected = _run_two_phase_wizard(available)

    write_human_line(_RELAUNCH_HINT)

    if not selected:
        write_human_line("No plugins selected.")
        return

    command = build_uv_tool_install_add_many(receipt, selected)

    write_human_line("Installing plugins: {}", ", ".join(selected))
    with ConcurrencyGroup(name="install-wizard") as cg:
        try:
            cg.run_process_to_completion(command)
        except ProcessError as e:
            raise AbortError(
                f"Failed to install plugins: {e.stderr.strip() or e.stdout.strip()}",
            ) from e

    write_human_line("Installed {} plugin(s): {}", len(selected), ", ".join(selected))

    # Enable all entry points from the selected packages in user config.
    # A single package may have multiple entry points (e.g. imbue-mngr-claude
    # provides claude, code_guardian, fixme_fairy, headless_claude).
    _enable_selected_plugins(frozenset(selected))


@click.command(name="install-wizard")
@add_common_options
@click.pass_context
def install_wizard(ctx: click.Context, **kwargs: Any) -> None:
    try:
        install_wizard_impl()
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


CommandHelpMetadata(
    key="plugin.install-wizard",
    one_line_description="Interactive wizard to install recommended plugins",
    synopsis="mngr plugin install-wizard",
    description="""Presents a two-phase TUI for selecting plugins to install.

Phase 1 shows agent types and providers (BASIC tier). Tools detected
on your system are pre-selected.

Phase 2 shows optional extras, filtered to only include extras related
to the agent types you selected. Recommended extras are pre-selected.

Use Space to toggle selections, Enter to confirm, and q or Ctrl+C
to cancel.""",
    examples=(("Launch the plugin install wizard", "mngr plugin install-wizard"),),
    see_also=(
        ("plugin add", "Install a plugin package"),
        ("plugin list", "List discovered plugins"),
    ),
).register()
add_pager_help_option(install_wizard)
