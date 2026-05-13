"""Single-select urwid picker for CLI prompts.

Replaces text-mode prompts (`[y/n]:`, `[1-N]:`) in the `mngr extras`
subcommands with a navigable TUI list. Modeled on the multi-select
checkbox screen in `plugin_install_wizard._run_selection_screen` but
single-select: Enter immediately confirms the focused row.
"""

from collections.abc import Sequence

from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.event_loop.main_loop import MainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.divider import Divider
from urwid.widget.frame import Frame
from urwid.widget.listbox import ListBox
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.pile import Pile
from urwid.widget.text import Text
from urwid.widget.wimp import SelectableIcon

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.cli.urwid_utils import create_urwid_screen_preserving_terminal


class _PickerState(MutableModel):
    """Mutable state shared with the input filter.

    Holds only the confirm flag the input filter needs to flip; the
    listbox itself is owned by ``run_single_select_picker`` as a local.
    """

    is_confirmed: bool = False


class _PickerInputFilter(MutableModel):
    """Intercepts Enter (confirm) and q/Ctrl+C (cancel)."""

    state: _PickerState

    def __call__(self, keys: list[str], raw: list[int]) -> list[str]:
        passthrough: list[str] = []
        for key in keys:
            if key == "enter":
                self.state.is_confirmed = True
                raise ExitMainLoop()
            if key in ("q", "Q", "ctrl c"):
                raise ExitMainLoop()
            passthrough.append(key)
        return passthrough


def run_single_select_picker(
    options: Sequence[str],
    *,
    title: str,
    header_text: str,
    initial_focus: int = 0,
) -> int | None:
    """Show a numbered single-select urwid picker.

    Returns the index of the selected option, or None if the user
    cancelled (q / Ctrl+C). Caller is responsible for ensuring an
    interactive terminal is available before calling.
    """
    if not options:
        return None

    # SelectableIcon (not Text) is required so ListBox arrow-key navigation
    # can move focus between rows -- Text widgets aren't selectable, which
    # leaves focus stuck on the first row and Up/Down silently no-ops.
    # urwid's SelectableIcon.get_cursor_coords returns None iff
    # cursor_position > len(text), hiding the cursor. The text rendered
    # below is `"  " + label` (length len(label) + 2), so any value
    # strictly greater than len(label) + 2 hides the cursor; `+ 4` is
    # used as defensive padding so a small refactor of the prefix can't
    # silently make the cursor reappear.
    list_items = [
        AttrMap(SelectableIcon(f"  {label}", cursor_position=len(label) + 4), None, focus_map="reversed")
        for label in options
    ]
    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker(list_items)
    listbox = ListBox(list_walker)
    if 0 <= initial_focus < len(options):
        listbox.set_focus(initial_focus)

    state = _PickerState()

    header = Pile(
        [
            AttrMap(Text(title, align="center"), "header"),
            Divider(),
            Text(header_text),
            Divider(),
        ]
    )

    footer = Pile(
        [
            Divider(),
            AttrMap(
                Text("  Up/Down: Navigate | Enter: Confirm | q/Ctrl+C: Cancel"),
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

    input_filter = _PickerInputFilter(state=state)

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

    return listbox.focus_position
