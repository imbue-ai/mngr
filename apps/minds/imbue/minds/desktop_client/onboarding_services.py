"""Service entries for the creating-page onboarding carousel.

The onboarding walkthrough on the creating page shows a scrolling strip of
the apps latchkey can connect to. The list comes from the bundled latchkey
services catalog (``services.json`` via :class:`ServicesCatalog`); each
entry pairs the human-readable service name with the bundled brand icon in
``static/service_icons/`` when one exists (see the README there -- services
without a bundled icon get a monogram fallback in the template).
"""

from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.services_catalog import ServicesCatalog

# Bundled brand icons, keyed by canonical service name (<service_id>.svg).
# Lives inside the package so it ships in the wheel with the rest of static/.
_SERVICE_ICON_DIR: Final[Path] = Path(__file__).resolve().parent / "static" / "service_icons"


class OnboardingService(FrozenModel):
    """One row of the onboarding carousel: a connectable service."""

    service_id: str
    display_name: str
    icon_url: str | None


@pure
def _cleaned_display_name(display_name: str) -> str:
    """Strip a trailing scope parenthetical, e.g. ``GitHub (REST API)`` -> ``GitHub``.

    Catalog display names disambiguate multiple scopes of one service; the
    carousel shows each service once, so the parenthetical is noise there.
    """
    if display_name.endswith(")") and " (" in display_name:
        return display_name[: display_name.rindex(" (")]
    return display_name


def list_onboarding_services(catalog: ServicesCatalog) -> tuple[OnboardingService, ...]:
    """Return the carousel entries, sorted by name and deduplicated.

    Deduplication is by cleaned display name (e.g. ``notion`` and
    ``notion-mcp`` both present as "Notion"), keeping the first service id
    in sorted order. ``icon_url`` is the ``/_static`` URL of the bundled
    brand icon, or ``None`` when no icon ships for the service.
    """
    entries: dict[str, OnboardingService] = {}
    for service_id in sorted(catalog.all_service_names()):
        infos = catalog.get(service_id)
        if not infos:
            continue
        name = _cleaned_display_name(infos[0].display_name)
        if name in entries:
            continue
        icon_url = (
            f"/_static/service_icons/{service_id}.svg" if (_SERVICE_ICON_DIR / f"{service_id}.svg").is_file() else None
        )
        entries[name] = OnboardingService(service_id=service_id, display_name=name, icon_url=icon_url)
    return tuple(sorted(entries.values(), key=lambda s: s.display_name.lower()))
