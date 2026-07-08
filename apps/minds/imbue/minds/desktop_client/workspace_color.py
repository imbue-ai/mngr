"""Workspace color palette and pure helpers.

The 10-color palette users can pick from (the chromatic entries from the
Figma source at node 356:4113), plus the WCAG luminance contrast picker
and the lenient hex normalizer the picker UI needs.

Pure black (``#000000``) and pure white (``#ffffff``) are intentionally
*not* in the palette: non-workspace minds screens now paint themselves
pure white (or pure black in dark mode) per the system theme, so a
workspace whose accent was black or white would be indistinguishable
from the neutral, workspace-less chrome. Users who really want one can
still type it into the settings hex input; only the preset swatches and
the auto-pick exclude them.

This module sits below ``templates.py`` in the import graph -- it has
no other minds imports -- so the ``BackendResolver`` (which is below
``templates`` because ``templates`` imports ``agent_creator`` which
imports ``backend_resolver``) can read the default color and normalize
stored labels without creating a cycle.

``normalize_workspace_color`` is mirrored as ``normalizeHex`` in
``static/workspace_accent.js`` for the picker pages' local input
validation. The palette itself is server-side only; a guard test in
``templates_test.py`` asserts the JS never reintroduces a palette mirror.
(Titlebar foreground contrast is no longer computed here -- the chrome
derives it from the workspace color in pure CSS; see ``.titlebar-surface``
in ``static/app.css``.)
"""

import re
from collections.abc import Collection
from collections.abc import Mapping
from typing import Final

from imbue.imbue_common.pure import pure

# Ten user-pickable workspace colors, all from the Figma source (Minds
# Early IA Explorations, node 356:4113). Names are kebab-case and are
# not surfaced visually in the UI today (the picker shows unlabeled
# swatches); they exist so code can refer to the default by name and as
# the swatches' screen-reader labels (the ColorSwatch aria-label).
#
# Order matters: the picker renders swatches in this order and
# ``pick_unused_create_color`` walks it to find the first free color.
# ``confusion`` (the default) leads. The achromatic neutrals (pure black
# and pure white) were removed deliberately -- see the module docstring.
WORKSPACE_PALETTE: Final[Mapping[str, str]] = {
    "confusion": "#0b292b",
    "courage": "#492222",
    "envy": "#3c3d06",
    "peace": "#9fbbd3",
    "belonging": "#e8a7a8",
    "energy": "#cecd0c",
    "strength": "#cfc7b3",
    "comfort": "#f5d6a0",
    "inspiration": "#e9ecd9",
    "clarity": "#fcefd4",
}

# Default workspace color: preselected on the create form when no other
# palette entry is suggested, and used as the renderer-side fallback for
# primary agents that have no ``color`` label on disk (pre-picker
# workspaces keep rendering as this until the user picks; nothing
# proactively writes the label).
DEFAULT_WORKSPACE_COLOR_NAME: Final[str] = "confusion"
DEFAULT_WORKSPACE_COLOR: Final[str] = WORKSPACE_PALETTE[DEFAULT_WORKSPACE_COLOR_NAME]

_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@pure
def pick_unused_create_color(used_colors: Collection[str]) -> str:
    """Pick the color to preselect in the create form.

    Returns ``DEFAULT_WORKSPACE_COLOR`` (confusion) when nothing is in
    use yet (no workspaces) or when every palette entry is already
    taken; otherwise returns the first palette entry (in palette order)
    not present in ``used_colors``. Custom (non-palette) colors in
    ``used_colors`` simply don't match any palette entry, so they never
    block a palette pick.

    ``used_colors`` should hold normalized ``#rrggbb`` lowercase hexes
    (the form the resolver + ``normalize_workspace_color`` emit); the
    comparison is case-insensitive regardless.
    """
    normalized_used = {c.lower() for c in used_colors}
    if not normalized_used:
        return DEFAULT_WORKSPACE_COLOR
    for hex_value in WORKSPACE_PALETTE.values():
        if hex_value not in normalized_used:
            return hex_value
    return DEFAULT_WORKSPACE_COLOR


@pure
def normalize_workspace_color(value: str) -> str | None:
    """Lenient hex parser for workspace color inputs.

    Accepts ``#fff`` / ``fff`` / ``#ffffff`` / ``ffffff`` in any case
    (with leading/trailing whitespace tolerated). Returns the canonical
    ``#rrggbb`` lowercase form on success, or ``None`` if the input is
    not a recognized 3- or 6-character hex literal. Alpha channel
    inputs (``#rrggbbaa``) are rejected; the picker UI does not offer
    them and they would propagate as invisible chrome.
    """
    match = _HEX_PATTERN.match(value.strip())
    if not match:
        return None
    body = match.group(1).lower()
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return f"#{body}"
