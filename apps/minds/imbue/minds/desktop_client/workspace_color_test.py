from typing import Final

import pytest

from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR_NAME
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.desktop_client.workspace_color import normalize_workspace_color
from imbue.minds.desktop_client.workspace_color import pick_unused_create_color

# Order is significant: it drives the picker's render order and
# pick_unused_create_color's preference walk. ``confusion`` (the
# default) leads; pure black and pure white are intentionally absent
# (the neutral system-theme chrome would collide with them).
_EXPECTED_PALETTE: Final[dict[str, str]] = {
    "confusion": "#0b292b",
    "courage": "#492222",
    "envy": "#3c3d06",
    "peace": "#9fbbd3",
    "belonging": "#e8a7a8",
    "energy": "#cecd0c",
    "strength": "#cfc7b3",
    "comfort": "#f5d6a0",
    "template": "#e9ecd9",
    "clarity": "#fcefd4",
}


def test_workspace_palette_matches_expected_entries() -> None:
    # Pinning the exact entries *and their order* here so a stray edit to
    # workspace_color.py (rename / typo / dropped entry / reorder) fails
    # loudly -- order drives both the picker's render order and
    # pick_unused_create_color's preference walk, so an order-insensitive
    # dict comparison would let a reorder slip through.
    assert list(WORKSPACE_PALETTE.items()) == list(_EXPECTED_PALETTE.items())


def test_workspace_palette_excludes_pure_black_and_white() -> None:
    # Pure black/white were removed so a workspace accent can't collide
    # with the neutral system-theme chrome (which is now pure white in
    # light mode / pure black in dark mode). Users can still type either
    # into the settings hex input; they're just not preset swatches.
    values = set(WORKSPACE_PALETTE.values())
    assert "#000000" not in values
    assert "#ffffff" not in values
    # ``confusion`` (the default) still leads the palette.
    assert list(WORKSPACE_PALETTE.keys())[0] == "confusion"


def test_default_workspace_color_is_confusion() -> None:
    assert DEFAULT_WORKSPACE_COLOR_NAME == "confusion"
    assert DEFAULT_WORKSPACE_COLOR == WORKSPACE_PALETTE["confusion"]
    assert DEFAULT_WORKSPACE_COLOR == "#0b292b"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#ffffff", "#ffffff"),
        ("ffffff", "#ffffff"),
        ("#FFFFFF", "#ffffff"),
        ("FFFFFF", "#ffffff"),
        ("#fff", "#ffffff"),
        ("fff", "#ffffff"),
        ("#FFF", "#ffffff"),
        ("#0b292b", "#0b292b"),
        ("0B292B", "#0b292b"),
        ("  #fff  ", "#ffffff"),
        ("\tffffff\n", "#ffffff"),
    ],
)
def test_normalize_workspace_color_accepts_lenient_inputs(value: str, expected: str) -> None:
    assert normalize_workspace_color(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-hex",
        "#ff",
        "#fffff",
        "#fffffff",
        "#xyz",
        "#ffffff80",
        "rgb(255, 255, 255)",
        "ffffffff",
    ],
)
def test_normalize_workspace_color_rejects_malformed_inputs(value: str) -> None:
    assert normalize_workspace_color(value) is None


# -- pick_unused_create_color --------------------------------------------
#
# The create form preselects the first palette color not already used by
# an existing workspace, falling back to confusion when nothing is in use
# yet or every palette entry is taken.

_PALETTE_HEXES: Final[tuple[str, ...]] = tuple(WORKSPACE_PALETTE.values())
_CONFUSION = WORKSPACE_PALETTE["confusion"]


def test_pick_unused_create_color_defaults_to_confusion_when_none_used() -> None:
    # No workspaces yet -> the named default (confusion, which also leads
    # the palette).
    assert pick_unused_create_color(set()) == _CONFUSION


def test_pick_unused_create_color_returns_confusion_when_all_used() -> None:
    assert pick_unused_create_color(set(_PALETTE_HEXES)) == _CONFUSION


def test_pick_unused_create_color_returns_first_unused_in_palette_order() -> None:
    # Confusion is used (e.g. one label-less workspace renders as confusion);
    # the first unused palette entry in order is courage (confusion leads
    # the chromatic block, so the next one is courage -- not a neutral).
    assert pick_unused_create_color({_CONFUSION}) == WORKSPACE_PALETTE["courage"]


def test_pick_unused_create_color_skips_to_next_unused() -> None:
    # confusion + courage taken -> next chromatic palette entry is envy.
    assert pick_unused_create_color({_CONFUSION, WORKSPACE_PALETTE["courage"]}) == WORKSPACE_PALETTE["envy"]


def test_pick_unused_create_color_ignores_custom_colors() -> None:
    # A custom (non-palette) color in use doesn't block any palette pick;
    # with a custom color the set is non-empty so the first palette entry
    # (confusion) is returned.
    assert pick_unused_create_color({"#123456"}) == _CONFUSION


def test_pick_unused_create_color_is_case_insensitive() -> None:
    # Uppercased used colors still match palette entries.
    used = {_CONFUSION.upper()}
    assert pick_unused_create_color(used) == WORKSPACE_PALETTE["courage"]
