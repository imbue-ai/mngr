"""Design system tokens for the minds desktop client.

Centralizes the workspace color palette, theme inference, and the OKLCH
migration helper used to seed pre-existing agents with a deterministic
starting color.

The 11 named presets come from the Figma "Minds Early IA Explorations"
file (node 314:4141); a workspace color persisted per-agent in
``MindsConfig`` may be either one of these preset slugs or an arbitrary
CSS color literal (so the eventual freeform-picker follow-up needs no
storage migration). Theme (dark vs light) is inferred from the chosen
color's perceptual lightness using the HSP brightness model; WCAG
relative luminance underweights blue and would miscategorize the pastel
blue/pink presets, so HSP is used here.
"""

import hashlib
import re
from enum import StrEnum
from math import sqrt
from typing import Any
from typing import Final
from typing import Mapping
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema


class WorkspacePreset(StrEnum):
    """The 11 named background-color presets surfaced by the workspace-color picker."""

    INDIFFERENCE = "indifference"
    CONFUSION = "confusion"
    COURAGE = "courage"
    ENVY = "envy"
    PEACE = "peace"
    BELONGING = "belonging"
    ENERGY = "energy"
    STRENGTH = "strength"
    COMFORT = "comfort"
    INSPIRATION = "inspiration"
    CLARITY = "clarity"


class Theme(StrEnum):
    """Auto-flipping foreground theme inferred from the workspace bg color."""

    DARK = "dark"
    LIGHT = "light"


WORKSPACE_PRESETS: Final[Mapping[WorkspacePreset, str]] = {
    WorkspacePreset.INDIFFERENCE: "#000000",
    WorkspacePreset.CONFUSION: "#0B292B",
    WorkspacePreset.COURAGE: "#492222",
    WorkspacePreset.ENVY: "#3C3D06",
    WorkspacePreset.PEACE: "#9FBBD3",
    WorkspacePreset.BELONGING: "#E8A7A8",
    WorkspacePreset.ENERGY: "#CECD0C",
    WorkspacePreset.STRENGTH: "#CFC7B3",
    WorkspacePreset.COMFORT: "#F5D6A0",
    WorkspacePreset.INSPIRATION: "#E9ECD9",
    WorkspacePreset.CLARITY: "#FCEFD4",
}

# Catch any drift between the enum and the palette table at import time.
assert set(WORKSPACE_PRESETS.keys()) == set(WorkspacePreset)

DEFAULT_WORKSPACE_PRESET: Final[WorkspacePreset] = WorkspacePreset.CONFUSION

# OKLCH starting-color tuning. Lightness 75% (was 65% for the legacy
# accent-stripe derivation) so the result reads as a full-background
# color, not a thin accent.
_OKLCH_STARTING_LIGHTNESS_PERCENT: Final[int] = 75
_OKLCH_STARTING_CHROMA: Final[float] = 0.15

# HSP brightness threshold for the dark/light theme cut. Empirically, 0.5
# splits the 11 presets cleanly.
_THEME_LIGHT_THRESHOLD: Final[float] = 0.5

_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_OKLCH_RE: Final[re.Pattern[str]] = re.compile(
    r"^oklch\(\s*(\d{1,3}(?:\.\d+)?)%\s+([\d.]+)\s+([\d.]+)(?:\s*/\s*[\d.%]+)?\s*\)$"
)
_RGB_RE: Final[re.Pattern[str]] = re.compile(
    r"^rgb\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*\)$"
)


class WorkspaceColor(str):
    """A user-pickable background color: preset slug or CSS color literal.

    Accepts either a :class:`WorkspacePreset` slug (e.g. ``"confusion"``) or
    a CSS color literal in one of ``#RRGGBB`` / ``#RRGGBBAA`` /
    ``oklch(L% C H)`` / ``rgb(r g b)`` form. Anything else raises
    ``ValueError``. Plays nicely with Pydantic via
    ``__get_pydantic_core_schema__`` so API request bodies can declare
    ``color: WorkspaceColor`` directly.
    """

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if not stripped:
            raise ValueError("WorkspaceColor cannot be empty")
        if stripped in _PRESET_SLUGS:
            return super().__new__(cls, stripped)
        if _HEX_RE.match(stripped) or _OKLCH_RE.match(stripped) or _RGB_RE.match(stripped):
            return super().__new__(cls, stripped)
        raise ValueError(
            "WorkspaceColor must be a preset slug or a CSS color literal "
            f"(#RRGGBB / #RRGGBBAA / oklch(L% C H) / rgb(r g b)), got: {value!r}"
        )

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1),
            serialization=core_schema.to_string_ser_schema(),
        )

    def is_preset(self, preset: WorkspacePreset | None = None) -> bool:
        """Whether the color is a preset slug (any preset, or a specific one)."""
        if preset is None:
            return str(self) in _PRESET_SLUGS
        return str(self) == preset.value

    def resolve_hex(self) -> str:
        """Resolve preset slugs to their hex value; pass literals through unchanged."""
        if self in _PRESET_SLUGS:
            return WORKSPACE_PRESETS[WorkspacePreset(str(self))]
        return str(self)


_PRESET_SLUGS: Final[frozenset[str]] = frozenset(p.value for p in WorkspacePreset)


def theme_for(color: WorkspaceColor) -> Theme:
    """Infer dark/light theme from a workspace color's perceptual lightness."""
    return Theme.LIGHT if _perceptual_lightness(color) >= _THEME_LIGHT_THRESHOLD else Theme.DARK


def oklch_starting_color(agent_id: str) -> WorkspaceColor:
    """Deterministic OKLCH starting color for the per-agent migration.

    Replaces the legacy 65%-lightness accent derivation. Lightness 75%
    keeps every starting color readable as a full background; the SHA-256
    hue is stable per agent so the same agent always gets the same
    starting color.
    """
    digest = hashlib.sha256(agent_id.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:4], "big") % 360
    return WorkspaceColor(f"oklch({_OKLCH_STARTING_LIGHTNESS_PERCENT}% {_OKLCH_STARTING_CHROMA} {hue})")


def _perceptual_lightness(color: WorkspaceColor) -> float:
    """Estimate perceptual lightness in [0, 1] for the dark/light theme cut.

    Preset slugs and hex/rgb literals: HSP brightness. OKLCH literals: the
    OKLCH L value directly (already perceptual; comparable to HSP for our
    threshold purpose).
    """
    raw = color.resolve_hex() if color in _PRESET_SLUGS else str(color)
    oklch_match = _OKLCH_RE.match(raw)
    if oklch_match is not None:
        return float(oklch_match.group(1)) / 100.0
    rgb_match = _RGB_RE.match(raw)
    if rgb_match is not None:
        r = int(rgb_match.group(1))
        g = int(rgb_match.group(2))
        b = int(rgb_match.group(3))
        return _hsp(r, g, b)
    hex_match = _HEX_RE.match(raw)
    if hex_match is not None:
        hex_str = hex_match.group(1)
        # Drop alpha if present; we evaluate the visible color only.
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        return _hsp(r, g, b)
    # Unreachable: the WorkspaceColor validator already gated all formats.
    raise ValueError(f"Cannot compute lightness for color: {raw!r}")


def _hsp(r: int, g: int, b: int) -> float:
    """HSP perceived-brightness formula (Darel Rex Finley)."""
    rn = r / 255.0
    gn = g / 255.0
    bn = b / 255.0
    return sqrt(0.299 * rn * rn + 0.587 * gn * gn + 0.114 * bn * bn)
