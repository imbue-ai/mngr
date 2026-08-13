"""The bundled service marks are each vendor's own artwork, carrying its own color.

These marks used to be simple-icons silhouettes: one path with no ``fill`` at
all, colored by whatever drew them. They are now drawn as ``<img>`` (see
``frontend/src/views/components/ServiceMark.ts``), and an ``<img>``-loaded SVG
is an isolated document that inherits no color from the page -- so a silhouette
that finds its way back in here paints flat black rather than a brand color,
and does it silently. Every check below is chosen to fail on exactly that.
"""

import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Final

SERVICE_ICON_DIR: Final[Path] = Path(__file__).resolve().parent / "static" / "service_icons"

DARK_SURFACE_SUFFIX: Final[str] = "-on-dark"

# Marks drawn in a single ink because that is how the brand draws them --
# whether that ink is black (GitHub), a brand color (Claude's coral, Coolify's
# purple), or near-black (ngrok, Ramp) -- plus the two services no color
# artwork exists for anywhere (Calendly and Umami are ``palette: false`` in
# every Iconify set that carries them, so they keep the flat simple-icons
# silhouette with its fill written in). Pinned by name so that swapping any
# *other* mark for a flat one fails here.
SINGLE_INK_MARKS: Final[frozenset[str]] = frozenset(
    {
        "calendly",
        "claude-ai",
        "coolify",
        "discord",
        "dropbox",
        "github",
        "linear",
        "ngrok",
        "ramp",
        "sentry",
        "umami",
        "yelp",
    }
)

# Services whose mark is near-black and vanishes on the dark theme's #000
# surface, and that a vendor publishes a white variant of. This end of the
# chain pins the list to the files on disk; ServiceMark.test.ts pins the same
# list to ``DARK_SURFACE_VARIANT_SERVICE_NAMES``, the set that decides when to
# render the second <img>. A variant asked for without artwork, or shipped
# without being asked for, fails on one side or the other.
DARK_SURFACE_VARIANT_SERVICES: Final[frozenset[str]] = frozenset(
    {"aws", "github", "linear", "ngrok", "ramp", "sentry", "umami"}
)

_PAINT_ATTR_RE: Final[re.Pattern[str]] = re.compile(r'(?:fill|stroke|stop-color)="([^"]+)"')
_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# Elements that paint, and so fall back to black when nothing gives them a fill.
_PAINTING_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<(?:path|circle|ellipse|rect|polygon|polyline|line)\b[^>]*>")


def _mark_paths() -> tuple[Path, ...]:
    return tuple(sorted(SERVICE_ICON_DIR.glob("*.svg")))


def _declared_inks(text: str) -> frozenset[str]:
    """The literal colors named by a paint attribute, lowercased."""
    return frozenset(value.lower() for value in _PAINT_ATTR_RE.findall(text) if _HEX_RE.match(value))


def _inks(text: str) -> frozenset[str]:
    """Every color the file actually paints with.

    A painting element with no fill of its own and no fill on the root paints
    black -- Notion's mark does exactly this, deliberately, pairing an unfilled
    path with a white one -- so black counts as an ink even when unwritten.
    """
    declared = _declared_inks(text)
    root_fill = ElementTree.fromstring(text).attrib.get("fill")
    is_implicitly_black = root_fill is None and any(
        "fill=" not in match.group(0) for match in _PAINTING_TAG_RE.finditer(text)
    )
    return declared | ({"#000000"} if is_implicitly_black else frozenset())


def test_every_mark_declares_its_own_color() -> None:
    """No mark may rely on the page for its color, and every one parses.

    A fill-less silhouette is the specific regression this guards: it has zero
    paint attributes, so it fails the ink check, and ``currentColor`` fails the
    other -- through an ``<img>`` it resolves to the document's initial color
    (black) rather than reaching the page.
    """
    paths = _mark_paths()
    assert len(paths) >= 30, f"expected the full mark set, found {len(paths)}"
    for path in paths:
        text = path.read_text(encoding="utf-8")
        root = ElementTree.fromstring(text)
        assert root.tag.endswith("svg"), f"{path.name} is not an SVG document"
        assert "viewBox" in root.attrib, f"{path.name} has no viewBox, so it cannot be scaled"
        assert "currentColor" not in text, f"{path.name} defers its color to the page"
        assert _inks(text), f"{path.name} declares no color of its own"


def test_only_the_brands_that_draw_themselves_flat_are_single_color() -> None:
    """Guards a silent swap back to monochrome for any *other* service.

    A revert of, say, ``slack.svg`` to a one-color silhouette still declares a
    fill, so the check above would pass it; this one fails on the set.
    """
    single_ink = {
        path.stem
        for path in _mark_paths()
        if not path.stem.endswith(DARK_SURFACE_SUFFIX) and len(_inks(path.read_text(encoding="utf-8"))) == 1
    }
    assert single_ink == set(SINGLE_INK_MARKS)


def test_dark_surface_variants_are_the_vendors_white_artwork() -> None:
    """The second file for a mark that vanishes on black, and only that.

    ServiceMark.ts renders the variant without an ``onerror`` probe, so a
    missing file there would show as nothing rather than as a fallback. This is
    what closes that hole. White-only is asserted because a variant that shares
    the base mark's near-black ink would defeat its whole purpose -- and the
    only permitted way to get one is a vendor-published white logo, never a
    recolored copy of the base.
    """
    variants = {
        path.stem[: -len(DARK_SURFACE_SUFFIX)] for path in _mark_paths() if path.stem.endswith(DARK_SURFACE_SUFFIX)
    }
    assert variants == set(DARK_SURFACE_VARIANT_SERVICES)
    for service in sorted(variants):
        variant = SERVICE_ICON_DIR / f"{service}{DARK_SURFACE_SUFFIX}.svg"
        assert _inks(variant.read_text(encoding="utf-8")) == frozenset({"#fff"})
        assert (SERVICE_ICON_DIR / f"{service}.svg").is_file(), f"{service} has a variant but no base mark"


def test_attribution_for_the_cc_by_artwork_ships_beside_it() -> None:
    """CC BY 4.0 obliges us to credit selfh.st wherever the marks ship.

    The file sits inside the package, so it travels in the wheel with the
    artwork it credits rather than only in the repo.
    """
    attribution = (SERVICE_ICON_DIR / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "selfh.st" in attribution
    assert "CC BY 4.0" in attribution
    assert "creativecommons.org/licenses/by/4.0" in attribution
