"""The keybinding chord grammar: the canonical spelling every stored chord uses (spec section 10.1).

A chord is lowercase tokens joined with ``+``: the held modifiers in one fixed
order (ctrl, alt, shift, meta), then exactly one key -- a single printable
character, or a named key spelled as its lowercased ``KeyboardEvent.key`` value
(the built-in apps handle raw keydown events, so their names are the aligned
vocabulary). ``cmd`` is a recorder-side alias normalized to ``meta``; the
canonical form never contains it.

The template's ``workspace_profile.keybindings`` is the same grammar on the
reading side; ``workspace_profile_contract.json`` pins the two against one
fixture so a vocabulary change on either side fails the other until mirrored.
The desktop validates on write (the ``account-settings`` route), so nothing
non-canonical ever enters the account value or the snapshot.
"""

import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure

# The held modifiers, in the one order a canonical chord spells them.
CHORD_MODIFIER_ORDER: Final[tuple[str, ...]] = ("ctrl", "alt", "shift", "meta")

# The named keys a chord may end in, each the lowercased KeyboardEvent.key value
# ("plus" stands in for the literal "+", which the separator makes unspellable).
CHORD_NAMED_KEYS: Final[tuple[str, ...]] = (
    "enter",
    "escape",
    "tab",
    "space",
    "backspace",
    "delete",
    "insert",
    "home",
    "end",
    "pageup",
    "pagedown",
    "arrowup",
    "arrowdown",
    "arrowleft",
    "arrowright",
    "plus",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "f11",
    "f12",
)

# The app name Imbue Studio's own shortcuts are stored under (the keyboard-shortcuts spec, section 8.2). Only the
# chrome edits that entry; a workspace's own saves carry the stored one through.
IMBUE_STUDIO_KEYBINDINGS_APP: Final[str] = "imbue-studio"

# The chords no binding may claim: Ctrl+Alt+Del, which Windows keeps for itself, refused on every
# platform so one account's bindings mean the same everywhere (the Windows key needs no entry:
# it is not a modifier of the grammar).
REFUSED_CHORDS: Final[tuple[str, ...]] = ("alt+meta+delete",)

# The most chords one action may hold: the embed contract's MAX_CHORDS_PER_ACTION, past which a receiver drops the
# whole shortcuts message (pinned to embed_contract.js by keybindings_test.py).
MAX_CHORDS_PER_ACTION: Final[int] = 16

# A single printable character key: anything but whitespace and "+" (spelled "plus").
_SINGLE_CHARACTER_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[^\s+]$")

# App names and action ids as the manifests declare them.
_APP_OR_ACTION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@pure
def _is_canonical_key(token: str) -> bool:
    if token in CHORD_NAMED_KEYS:
        return True
    if _SINGLE_CHARACTER_KEY_RE.match(token) is not None:
        return token == token.lower()
    return False


@pure
def is_canonical_chord(chord: str) -> bool:
    """Whether the chord is already in the canonical spelling the grammar stores."""
    if not chord:
        return False
    tokens = chord.split("+")
    modifiers, key = tokens[:-1], tokens[-1]
    if not _is_canonical_key(key):
        return False
    modifier_indexes = []
    for modifier in modifiers:
        if modifier not in CHORD_MODIFIER_ORDER:
            return False
        modifier_indexes.append(CHORD_MODIFIER_ORDER.index(modifier))
    is_ordered_without_repeats = modifier_indexes == sorted(set(modifier_indexes))
    return is_ordered_without_repeats


@pure
def is_refused_chord(chord: str) -> bool:
    """Whether the chord is one no binding may claim (``REFUSED_CHORDS``)."""
    return chord in REFUSED_CHORDS


@pure
def describe_keybindings_value_problem(value: object) -> str | None:
    """Why the value is not a valid keybindings map, or None when it is.

    The valid shape is app name, then action id, then a list of at most
    ``MAX_CHORDS_PER_ACTION`` canonical chords none of which is refused (an
    empty list unbinds the action).
    """
    if not isinstance(value, Mapping):
        return f"keybindings must be an object of apps, not {type(value).__name__}"
    for app_name, chords_by_action in value.items():
        if not isinstance(app_name, str) or _APP_OR_ACTION_ID_RE.match(app_name) is None:
            return f"{app_name!r} is not an app name"
        if not isinstance(chords_by_action, Mapping):
            return f"the {app_name} entry must be an object of actions"
        for action_id, chords in chords_by_action.items():
            if not isinstance(action_id, str) or _APP_OR_ACTION_ID_RE.match(action_id) is None:
                return f"{action_id!r} is not an action id"
            if isinstance(chords, str) or not isinstance(chords, (list, tuple)):
                return f"{app_name}.{action_id} must assign a list of chords"
            if len(chords) > MAX_CHORDS_PER_ACTION:
                return f"{app_name}.{action_id} assigns more than {MAX_CHORDS_PER_ACTION} chords"
            for chord in chords:
                if not isinstance(chord, str) or not is_canonical_chord(chord):
                    return f"{chord!r} is not a canonical chord"
                if is_refused_chord(chord):
                    return f"{chord!r} belongs to the operating system"
    return None


@pure
def describe_keybindings_write_problem(value: object) -> str | None:
    """Why the value is not a valid keybindings write, or None when it is.

    A write is shaped like the stored map, except that an action may assign None,
    which removes its stored assignment so its defaults apply again.
    """
    if not isinstance(value, Mapping):
        return f"keybindings must be an object of apps, not {type(value).__name__}"
    with_removals_as_empty = {
        app_name: (
            {action_id: [] if chords is None else chords for action_id, chords in chords_by_action.items()}
            if isinstance(chords_by_action, Mapping)
            else chords_by_action
        )
        for app_name, chords_by_action in value.items()
    }
    return describe_keybindings_value_problem(with_removals_as_empty)


@pure
def is_usable_chord(chord: str) -> bool:
    """Whether a binding can hold the chord: canonical and not refused."""
    return is_canonical_chord(chord) and not is_refused_chord(chord)


@pure
def usable_keybindings(value: Mapping[str, Mapping[str, Sequence[str]]]) -> dict[str, dict[str, list[str]]]:
    """The keybindings without what no binding can use, so a stale entry never blocks a save or reaches a matcher.

    Drops apps and actions not named by the id pattern, and chords off the
    grammar or refused (a record can predate a refusal, or come from a client
    with a newer grammar), and keeps an action's first
    ``MAX_CHORDS_PER_ACTION`` chords. An action left with none stays, turned
    off, as its stored list asked.
    """
    return {
        app_name: {
            action_id: [chord for chord in chords if is_usable_chord(chord)][:MAX_CHORDS_PER_ACTION]
            for action_id, chords in chords_by_action.items()
            if _APP_OR_ACTION_ID_RE.match(action_id) is not None
        }
        for app_name, chords_by_action in value.items()
        if _APP_OR_ACTION_ID_RE.match(app_name) is not None
    }


@pure
def merge_keybindings_write(
    stored: Mapping[str, Mapping[str, Sequence[str]]],
    write: Mapping[str, Mapping[str, Sequence[str] | None]],
) -> dict[str, dict[str, list[str]]]:
    """The stored keybindings with a write applied per app and action.

    An assigned list replaces that action's stored one (an empty list turns the
    action off); None removes the action's stored assignment, and an app left
    with none is dropped. Apps and actions the write does not name are kept.
    """
    app_names = list(stored) + [app_name for app_name in write if app_name not in stored]
    merged_by_app = {
        app_name: _merged_actions(stored.get(app_name, {}), write.get(app_name, {})) for app_name in app_names
    }
    return {app_name: chords_by_action for app_name, chords_by_action in merged_by_app.items() if chords_by_action}


@pure
def _merged_actions(
    stored_actions: Mapping[str, Sequence[str]],
    written_actions: Mapping[str, Sequence[str] | None],
) -> dict[str, list[str]]:
    kept = {
        action_id: list(chords) for action_id, chords in stored_actions.items() if action_id not in written_actions
    }
    assigned = {action_id: list(chords) for action_id, chords in written_actions.items() if chords is not None}
    return {**kept, **assigned}


@pure
def with_stored_imbue_studio_keybindings(
    written: Mapping[str, Mapping[str, Sequence[str]]],
    stored: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, dict[str, list[str]]]:
    """A workspace's whole-map write with Imbue Studio's entry taken from what is stored, never from the write.

    The workspace builds its write from its snapshot, which may predate the
    chrome's latest save of Imbue Studio's own shortcuts.
    """
    workspace_apps = {
        app_name: {action_id: list(chords) for action_id, chords in chords_by_action.items()}
        for app_name, chords_by_action in written.items()
        if app_name != IMBUE_STUDIO_KEYBINDINGS_APP
    }
    stored_entry = stored.get(IMBUE_STUDIO_KEYBINDINGS_APP)
    if stored_entry is None:
        return workspace_apps
    return {
        **workspace_apps,
        IMBUE_STUDIO_KEYBINDINGS_APP: {action_id: list(chords) for action_id, chords in stored_entry.items()},
    }
