"""minds-specific ratchets (patterns that only make sense for this project).

Repo-wide ratchets live in ``test_ratchets.py`` (same test set across all
projects, enforced by ``test_meta_ratchets.py``); this file uses the core
ratchet API directly for desktop-client-only conventions.
"""

from pathlib import Path

from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet
from imbue.imbue_common.ratchet_testing.core import format_ratchet_failure_message

_DESKTOP_CLIENT_DIR = Path(__file__).parent / "desktop_client"

# Hand-rolled DOM building in the desktop client's classic-script surfaces.
# The chrome/modal UI renders through mithril components in
# ``apps/minds/frontend/src`` (see specs/minds-chrome-mithril-migration);
# ``createElement`` / ``.innerHTML`` / ``insertAdjacentHTML`` in the remaining
# static JS and inline template scripts is the pattern that migration deleted,
# locked here at the post-migration count so it cannot silently return. New or
# reworked UI belongs in the component bundle (which may still innerHTML-swap
# server-rendered fragments -- e.g. the inbox detail pane -- deliberately
# outside this ratchet's scope).
_DOM_BUILDING_PATTERN = RegexPattern(r"createElement\(|\.innerHTML|insertAdjacentHTML")
_DOM_BUILDING_RULE_NAME = "hand-rolled DOM building outside the component bundle"
_DOM_BUILDING_RULE_DESCRIPTION = (
    "Chrome/modal UI is rendered by the mithril components in apps/minds/frontend/src; "
    "building DOM by hand (createElement / .innerHTML / insertAdjacentHTML) in static JS "
    "or inline template scripts recreates the dual-rendering problem the migration removed. "
    "Add or rework UI as a component in the bundle instead."
)


def test_prevent_dom_building_in_static_js() -> None:
    chunks = check_regex_ratchet(
        _DESKTOP_CLIENT_DIR / "static",
        FileExtension(".js"),
        _DOM_BUILDING_PATTERN,
        # Vendored third-party bundle; not ours to ratchet.
        excluded_path_patterns=("*.min.js",),
    )
    assert len(chunks) <= snapshot(19), format_ratchet_failure_message(
        rule_name=_DOM_BUILDING_RULE_NAME,
        rule_description=_DOM_BUILDING_RULE_DESCRIPTION,
        chunks=chunks,
    )


def test_prevent_dom_building_in_template_scripts() -> None:
    chunks = check_regex_ratchet(
        _DESKTOP_CLIENT_DIR / "templates",
        FileExtension(".jinja"),
        _DOM_BUILDING_PATTERN,
    )
    assert len(chunks) <= snapshot(2), format_ratchet_failure_message(
        rule_name=_DOM_BUILDING_RULE_NAME,
        rule_description=_DOM_BUILDING_RULE_DESCRIPTION,
        chunks=chunks,
    )
