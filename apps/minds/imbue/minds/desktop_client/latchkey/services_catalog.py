"""Loads the latchkey-service-to-detent-schema mapping shipped with minds.

This catalog is desktop-only -- it tells the permission dialog which
schemas to render for a given latchkey service name. Agents do not see
this file; they only emit the service name and a rationale.

Defaults are not maintained per-service: every service implicitly defaults
to the detent ``any`` schema (matches every request inside the scope), so
clicking Approve without changing anything yields ``{<scope>: ["any"]}`` --
unrestricted access for the chosen service. The user can tighten this by
unticking ``any`` and selecting specific permissions in the dialog.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

_DEFAULT_CATALOG_PATH: Final[Path] = Path(__file__).resolve().parent / "services.toml"

# The detent ``any`` schema matches every request, so a rule like
# ``{"slack-api": ["any"]}`` allows all Slack access. We prepend ``any``
# to every service's permission list (deduplicated) so the dialog can
# render it as a checkbox, and pre-check it as the implicit default.
_IMPLICIT_DEFAULT_PERMISSION: Final[str] = "any"

IMPLICIT_DEFAULT_PERMISSIONS: Final[tuple[str, ...]] = (_IMPLICIT_DEFAULT_PERMISSION,)


class LatchkeyServicesCatalogError(Exception):
    """Base exception for catalog parsing/lookup failures."""


class MalformedServicesCatalogError(LatchkeyServicesCatalogError, ValueError):
    """Raised when the services catalog file is structurally invalid."""


class ServicePermissionInfo(FrozenModel):
    """Description of a single latchkey service's permission surface."""

    name: str = Field(description="Latchkey service name (e.g. 'slack', 'google-gmail').")
    display_name: str = Field(description="Human-readable label shown in the dialog header.")
    scope_schemas: tuple[str, ...] = Field(
        description="Detent scope schemas this service owns; used as keys in latchkey_permissions.json rules.",
    )
    permission_schemas: tuple[str, ...] = Field(
        description=(
            "Detent permission schemas the user can grant for this service. The implicit "
            "``any`` default is always present at index 0."
        ),
    )


def _build_service_info(name: str, raw: Mapping[str, object]) -> ServicePermissionInfo:
    """Turn a single TOML table into a ``ServicePermissionInfo``.

    Raises ``MalformedServicesCatalogError`` for shape violations so the
    runtime fails fast at startup rather than at request time.
    """
    display_name = raw.get("display_name")
    scope_schemas_raw = raw.get("scope_schemas")
    permission_schemas_raw = raw.get("permission_schemas")

    if not isinstance(display_name, str) or not display_name:
        raise MalformedServicesCatalogError(f"Service '{name}' must have a non-empty display_name")
    if not isinstance(scope_schemas_raw, list) or not all(isinstance(s, str) for s in scope_schemas_raw):
        raise MalformedServicesCatalogError(f"Service '{name}' scope_schemas must be a list of strings")
    if not scope_schemas_raw:
        raise MalformedServicesCatalogError(f"Service '{name}' scope_schemas must be non-empty")
    if not isinstance(permission_schemas_raw, list) or not all(isinstance(s, str) for s in permission_schemas_raw):
        raise MalformedServicesCatalogError(
            f"Service '{name}' permission_schemas must be a list of strings",
        )

    scope_schemas: tuple[str, ...] = tuple(str(s) for s in scope_schemas_raw)

    # Always make ``any`` available as the first checkbox, deduplicating in
    # case a service explicitly lists it (which is harmless but redundant).
    granular_permissions: tuple[str, ...] = tuple(str(s) for s in permission_schemas_raw)
    permission_schemas: tuple[str, ...] = (_IMPLICIT_DEFAULT_PERMISSION,) + tuple(
        p for p in granular_permissions if p != _IMPLICIT_DEFAULT_PERMISSION
    )

    return ServicePermissionInfo(
        name=name,
        display_name=display_name,
        scope_schemas=scope_schemas,
        permission_schemas=permission_schemas,
    )


def load_services_catalog(toml_path: Path | None = None) -> dict[str, ServicePermissionInfo]:
    """Load the catalog from disk, validating each entry.

    The default path points at the TOML file shipped with this package.
    """
    path = toml_path if toml_path is not None else _DEFAULT_CATALOG_PATH
    try:
        raw_bytes = path.read_bytes()
    except OSError as e:
        raise LatchkeyServicesCatalogError(f"Cannot read services catalog at {path}: {e}") from e

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise MalformedServicesCatalogError(f"Invalid TOML in services catalog at {path}: {e}") from e

    services_section = data.get("services")
    if not isinstance(services_section, dict):
        raise MalformedServicesCatalogError(f"Expected a [services] table at the top of {path}")

    catalog: dict[str, ServicePermissionInfo] = {}
    for service_name, raw in services_section.items():
        if not isinstance(raw, dict):
            raise MalformedServicesCatalogError(f"Service '{service_name}' must be a table")
        catalog[service_name] = _build_service_info(service_name, raw)

    logger.debug("Loaded latchkey services catalog with {} entries from {}", len(catalog), path)
    return catalog


def get_service_info(
    catalog: Mapping[str, ServicePermissionInfo],
    service_name: str,
) -> ServicePermissionInfo | None:
    """Return the catalog entry for ``service_name``, or ``None`` if unknown."""
    return catalog.get(service_name)
