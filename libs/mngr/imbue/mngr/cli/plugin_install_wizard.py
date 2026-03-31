"""Interactive plugin install wizard for mngr.

Presents available plugins in a TUI and lets the user select which
ones to install.  Selected plugins are installed in a single
``uv tool install`` invocation.
"""

from typing import Any
from typing import Final

import click
from loguru import logger
from pydantic import ConfigDict
from urwid.display.raw import Screen
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
from imbue.mngr.plugin_catalog import CatalogEntry
from imbue.mngr.plugin_catalog import SIGNAL_CHECKS
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
def _get_selected_package_names(
    plugins: tuple[CatalogEntry, ...],
    checkboxes: list[CheckBox],
) -> list[str]:
    """Extract the package names of all checked plugins."""
    return [plugin.package_name for plugin, cb in zip(plugins, checkboxes, strict=True) if cb.get_state()]


@pure
def _filter_already_installed(
    plugins: tuple[CatalogEntry, ...],
    installed_names: frozenset[str],
) -> tuple[CatalogEntry, ...]:
    """Remove plugins that are already installed."""
    return tuple(p for p in plugins if p.package_name not in installed_names)


def _should_preselect(entry: CatalogEntry) -> bool:
    """Determine if a catalog entry should be preselected in the wizard.

    BASIC-tier entries are preselected by default. If a signal is specified,
    preselection depends on the signal check passing. EXTRA-tier entries
    are never preselected.
    """
    if entry.tier != PluginTier.BASIC:
        return False
    if entry.signal is None:
        return True
    signal_check = SIGNAL_CHECKS.get(entry.signal)
    if signal_check is None:
        return False
    return check_signal(signal_check)


def _run_install_wizard(plugins: tuple[CatalogEntry, ...]) -> list[str]:
    """Run the install wizard TUI.

    Returns the list of selected package names, or an empty list if cancelled.
    """
    name_width = max(len(p.package_name) for p in plugins)

    checkboxes: list[CheckBox] = []
    list_items: list[AttrMap] = []

    for plugin in plugins:
        label = f"{plugin.package_name.ljust(name_width)}  {plugin.description}"
        cb = CheckBox(label, state=_should_preselect(plugin))
        checkboxes.append(cb)
        list_items.append(AttrMap(cb, None, focus_map="reversed"))

    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker(list_items)
    listbox = ListBox(list_walker)

    state = _WizardState(checkboxes=checkboxes, plugins=plugins)

    header = Pile(
        [
            AttrMap(Text("Plugin Install Wizard", align="center"), "header"),
            Divider(),
            Text("mngr has a flexible plugin architecture. Here are some available\nplugins for you to install:"),
            Divider(),
        ]
    )

    footer = Pile(
        [
            Divider(),
            AttrMap(
                Text("  Space: Toggle | Up/Down: Navigate | Enter: Install | q/Ctrl+C: Cancel"),
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

    screen = Screen()
    screen.tty_signal_keys(intr="undefined")

    loop = MainLoop(
        frame,
        palette=palette,
        input_filter=input_filter,
        screen=screen,
    )
    loop.run()

    if not state.is_confirmed:
        return []

    return _get_selected_package_names(plugins, checkboxes)


_RELAUNCH_HINT: Final[str] = (
    "You can re-launch the plugin installation wizard with `mngr plugin install-wizard`.\n"
    "See `mngr plugin --help` for more information on plugins."
)


def _install_wizard_impl() -> None:
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

    selected = _run_install_wizard(available)

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


@click.command(name="install-wizard")
@add_common_options
@click.pass_context
def install_wizard(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _install_wizard_impl()
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


CommandHelpMetadata(
    key="plugin.install-wizard",
    one_line_description="Interactive wizard to install available plugins",
    synopsis="mngr plugin install-wizard",
    description="""Presents a TUI with available plugins and lets you select which
ones to install. Plugins are installed in a single operation.

BASIC-tier plugins are pre-selected when their signal check passes
(e.g. the CLI tool is found on PATH). Use Space to toggle selections,
Enter to confirm, and q or Ctrl+C to cancel.""",
    examples=(("Launch the plugin install wizard", "mngr plugin install-wizard"),),
    see_also=(
        ("plugin add", "Install a plugin package"),
        ("plugin list", "List discovered plugins"),
    ),
).register()
add_pager_help_option(install_wizard)
