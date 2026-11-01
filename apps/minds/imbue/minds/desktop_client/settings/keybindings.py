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
def describe_keybindings_value_problem(value: object) -> str | None:
    """Why the value is not a valid keybindings map, or None when it is.

    The valid shape is app name, then action id, then a list of canonical
    chords (an empty list unbinds the action).
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
            for chord in chords:
                if not isinstance(chord, str) or not is_canonical_chord(chord):
                    return f"{chord!r} is not a canonical chord"
    return None
