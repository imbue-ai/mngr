"""Workspace color palette and pure helpers.

The 12-color palette users can pick from (11 named entries from the Figma
source at node 356:4113 plus literal ``#ffffff`` white), plus the WCAG
luminance contrast picker and the lenient hex normalizer the picker UI
needs.

This module sits below ``templates.py`` in the import graph -- it has
no other minds imports -- so the ``BackendResolver`` (which is below
``templates`` because ``templates`` imports ``agent_creator`` which
imports ``backend_resolver``) can read the default color and normalize
stored labels without creating a cycle.

Mirrored in ``static/workspace_accent.js``; a drift-guard test in
``templates_test.py`` parses the JS file and asserts the two halves
stay in lockstep.
"""

import re
from collections.abc import Collection
from collections.abc import Mapping
from typing import Final

from imbue.imbue_common.pure import pure

# Twelve user-pickable workspace colors. Eleven named entries come from
# the Figma source (Minds Early IA Explorations, node 356:4113); the
# twelfth ("white") is added so users have a neutral light option
# distinct from the warm-cream Figma entries. Names are kebab-case and
# are not surfaced in the UI today (the picker shows unlabeled
# swatches); they exist so the system can refer to the default by
# name in code and so the same name list is auditable in both Python
# and JS.
WORKSPACE_PALETTE: Final[Mapping[str, str]] = {
    "indifference": "#000000",
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
    "white": "#ffffff",
}

# Default workspace color used at create time and for the one-time
# migration backfill applied to any primary agent that lacks a
# ``color`` label after the upgrade.
DEFAULT_WORKSPACE_COLOR_NAME: Final[str] = "confusion"
DEFAULT_WORKSPACE_COLOR: Final[str] = WORKSPACE_PALETTE[DEFAULT_WORKSPACE_COLOR_NAME]

# WCAG relative luminance threshold below which white text reads
# better than black against the background. The exact crossover is
# sqrt(1.05 * 0.05) - 0.05 ~= 0.1791; we use the rounded 0.179
# directly. The standard 0.03928 / 12.92 / 1.055 / 2.4 sRGB linearization
# numbers come from the WCAG 2.x relative-luminance definition.
_FOREGROUND_LUMINANCE_THRESHOLD: Final[float] = 0.179

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


@pure
def _srgb_to_linear(channel: float) -> float:
    """Inverse sRGB gamma. Used by ``pick_workspace_foreground``; kept at
    module scope (rather than nested) so the ratchet test that forbids
    nested defs in production code stays green."""
    if channel <= 0.03928:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


@pure
def pick_workspace_foreground(hex_color: str) -> str:
    """Return the contrasting RGB triple for titlebar text/icons over ``hex_color``.

    The returned value is ``"0 0 0"`` (black) or ``"255 255 255"``
    (white), suitable for dropping into ``rgb(var(--titlebar-fg) / <alpha>)``.
    Chooses by WCAG relative luminance so the picker stays legible across
    the whole 12-color palette and any custom hex -- replacing the prior
    fixed-OKLCH-L-85 picker that always emitted black.

    ``hex_color`` must be a normalized lowercase ``#rrggbb`` hex string.
    Callers should pass values through ``normalize_workspace_color`` first.
    """
    r = int(hex_color[1:3], 16) / 255.0
    g = int(hex_color[3:5], 16) / 255.0
    b = int(hex_color[5:7], 16) / 255.0
    luminance = 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)
    return "0 0 0" if luminance > _FOREGROUND_LUMINANCE_THRESHOLD else "255 255 255"
