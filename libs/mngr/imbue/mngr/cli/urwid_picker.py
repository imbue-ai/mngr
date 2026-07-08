"""urwid pickers for CLI prompts.

Replaces text-mode prompts (`[y/n]:`, `[1-N]:`) in the `mngr extras`
subcommands with a navigable TUI list. Provides a single-select picker
(Enter immediately confirms the focused row) and a multi-select picker
(Space toggles a checkbox per row, Enter confirms the set), both modeled
on the checkbox screen in `plugin_install_wizard._run_selection_screen`.
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
from urwid.widget.wimp import CheckBox
from urwid.widget.wimp import SelectableIcon

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mngr.errors import MismatchedPreselectionError


class _PickerState(MutableModel):
    """Mutable state shared with the input filter.

    Holds only the confirm flag the input filter needs to flip; the
    listbox (and its widgets) is owned by the calling picker function as
    a local.
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


def _run_picker_loop(
    listbox: ListBox,
    *,
    title: str,
    header_text: str,
    footer_text: str,
) -> _PickerState:
    """Run the shared urwid picker event loop around *listbox*.

    Wraps the chrome (header, footer, palette), the Enter/q/Ctrl+C input
    filter, and the screen-preserving ``MainLoop`` shared by every picker
    in this module. Returns the ``_PickerState`` so callers can check
    ``is_confirmed`` and then read their own selection off the listbox.
    """
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
            AttrMap(Text(footer_text), "status"),
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

    return state


def run_single_select_picker(
    options: Sequence[str],
    *,
    title: str,
    header_text: str,
    initial_focus: int = 0,
) -> int | None:
    """Show a single-select urwid picker.

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

    state = _run_picker_loop(
        listbox,
        title=title,
        header_text=header_text,
        footer_text="  Up/Down: Navigate | Enter: Confirm | q/Ctrl+C: Cancel",
    )

    if not state.is_confirmed:
        return None

    return listbox.focus_position


def run_multi_select_picker(
    options: Sequence[str],
    *,
    title: str,
    header_text: str,
    preselected: Sequence[bool] | None = None,
) -> list[int] | None:
    """Show a multi-select urwid picker with one checkbox per option.

    Space toggles the focused checkbox, Enter confirms the current set,
    and q / Ctrl+C cancels. Returns the indices of the checked options
    (possibly empty if the user confirmed with nothing checked), or None
    if the user cancelled. ``preselected`` (if given) must be the same
    length as ``options`` and sets the initial checked state of each row.
    Caller is responsible for ensuring an interactive terminal is
    available before calling.
    """
    if not options:
        return None

    if preselected is not None and len(preselected) != len(options):
        raise MismatchedPreselectionError("preselected must be the same length as options")
    if preselected is None:
        preselected = [False] * len(options)

    # The shared _PickerInputFilter intercepts Enter and q/Ctrl+C but lets
    # Space pass through to the focused CheckBox, which toggles on Space.
    checkboxes = [CheckBox(label, state=initial) for label, initial in zip(options, preselected, strict=True)]
    list_items = [AttrMap(cb, None, focus_map="reversed") for cb in checkboxes]
    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker(list_items)
    listbox = ListBox(list_walker)

    state = _run_picker_loop(
        listbox,
        title=title,
        header_text=header_text,
        footer_text="  Space: Toggle | Up/Down: Navigate | Enter: Confirm | q/Ctrl+C: Cancel",
    )

    if not state.is_confirmed:
        return None

    return [index for index, cb in enumerate(checkboxes) if cb.get_state()]
