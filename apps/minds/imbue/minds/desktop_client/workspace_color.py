"""Workspace color palette and pure helpers.

The 10-color palette users can pick from (the chromatic entries from the
Figma source at node 356:4113), plus the WCAG luminance contrast picker
and the lenient hex normalizer the picker UI needs.

Pure black (``#000000``) and pure white (``#ffffff``) are intentionally
*not* in the palette: non-workspace Imbue Studio screens paint white
surfaces (on a cream ground) in light mode and black in dark mode, so a
workspace whose accent was black or white would be indistinguishable
from the neutral, workspace-less chrome. Users who really want one can
still type it into the settings hex input; only the preset swatches and
the auto-pick exclude them.

This module sits at the bottom of the desktop client's import graph --
it has no other minds imports -- so anything from ``backend_resolver``
up through the ``ui_api_*`` route modules can read the default color and
normalize stored labels without creating a cycle.

``normalize_workspace_color`` is mirrored as ``normalizeWorkspaceColorHex``
in ``frontend/src/models/workspaceOptions.ts`` for the SPA color pickers'
local input validation. The palette itself is server-side only: the SPA
receives it through the workspace-options payload rather than keeping a
second copy in sync. (Titlebar foreground contrast is no longer computed
here -- the chrome derives it from the workspace color in pure CSS; see
``.titlebar-surface`` in ``frontend/src/style.css``.)
"""

import re
from collections import Counter
from collections.abc import Iterable
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
# ``pick_create_color`` walks it to find the least-used color.
# ``courage`` (the first run's brown, a new workspace's first suggestion)
# leads. The achromatic neutrals (pure black and pure white) were removed
# deliberately -- see the module docstring.
WORKSPACE_PALETTE: Final[Mapping[str, str]] = {
    "courage": "#492222",
    "confusion": "#0b292b",
    "envy": "#3c3d06",
    "peace": "#9fbbd3",
    "belonging": "#e8a7a8",
    "energy": "#cecd0c",
    "strength": "#cfc7b3",
    "comfort": "#f5d6a0",
    "template": "#e9ecd9",
    "clarity": "#fcefd4",
}

# The renderer-side fallback for primary agents that have no ``color``
# label on disk: pre-picker workspaces keep rendering as this until the
# user picks (nothing proactively writes the label), so it stays the color
# they always had.
DEFAULT_WORKSPACE_COLOR_NAME: Final[str] = "confusion"
DEFAULT_WORKSPACE_COLOR: Final[str] = WORKSPACE_PALETTE[DEFAULT_WORKSPACE_COLOR_NAME]

_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@pure
def pick_create_color(workspace_colors: Iterable[str]) -> str:
    """Pick the color to preselect in the create form: the palette entry the fewest workspaces wear.

    Ties go to palette order, so the suggestions run through the palette from ``courage`` and, once every entry
    is worn, around it again; a deleted workspace's color is the first to come back. ``workspace_colors`` holds
    every active workspace's color, repeats included. Custom (non-palette) colors match no entry and count for
    none. The comparison is case-insensitive.
    """
    worn = Counter(color.lower() for color in workspace_colors)
    return min(WORKSPACE_PALETTE.values(), key=lambda hex_value: worn[hex_value])


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
