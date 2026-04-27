"""Loads the latchkey-service-to-detent-schema mapping shipped with minds.

This catalog is desktop-only -- it tells the permission dialog which
schemas to render for a given latchkey service name and which to
pre-check by default. Agents do not see this file; they only emit the
service name and a rationale.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

_DEFAULT_CATALOG_PATH: Final[Path] = Path(__file__).resolve().parent / "services.toml"

_READ_ALL_SUFFIX: Final[str] = "-read-all"
_WRITE_ALL_SUFFIX: Final[str] = "-write-all"


class LatchkeyServicesCatalogError(Exception):
    """Base exception for catalog parsing/lookup failures."""


class MalformedServicesCatalogError(LatchkeyServicesCatalogError, ValueError):
    """Raised when the services catalog file is structurally invalid."""


class ServicePermissionInfo(FrozenModel):
    """Description of a single latchkey service's permission surface.

    ``default_permissions`` is what the permission dialog pre-checks the
    first time a service is requested. It is computed once at load time:
    if the TOML provides an explicit override, that override is used
    verbatim; otherwise the heuristic in ``_default_permissions_heuristic``
    runs. ``permission_schemas`` is the full list of schemas the dialog
    offers; the dialog itself enforces that any pre-checks are a subset.
    """

    name: str = Field(description="Latchkey service name (e.g. 'slack', 'google-gmail').")
    display_name: str = Field(description="Human-readable label shown in the dialog header.")
    description: str = Field(description="One-sentence summary shown in the dialog header.")
    scope_schemas: tuple[str, ...] = Field(
        description="Detent scope schemas this service owns; used as keys in permissions.json rules.",
    )
    permission_schemas: tuple[str, ...] = Field(
        description="Detent permission schemas the user can grant for this service.",
    )
    default_permissions: tuple[str, ...] = Field(
        description="Subset of permission_schemas that the dialog pre-checks on first open.",
    )


@pure
def _default_permissions_heuristic(permission_schemas: tuple[str, ...]) -> tuple[str, ...]:
    """Pick the ``-read-all`` / ``-write-all`` schemas as defaults.

    Falls back to the full list when neither suffix appears (e.g. for
    services like ``linear`` whose only permission schema is ``any``).
    """
    suffixed = tuple(
        schema
        for schema in permission_schemas
        if schema.endswith(_READ_ALL_SUFFIX) or schema.endswith(_WRITE_ALL_SUFFIX)
    )
    if suffixed:
        return suffixed
    return permission_schemas


def _build_service_info(name: str, raw: Mapping[str, object]) -> ServicePermissionInfo:
    """Turn a single TOML table into a ``ServicePermissionInfo``.

    Raises ``MalformedServicesCatalogError`` for shape violations so the
    runtime fails fast at startup rather than at request time.
    """
    display_name = raw.get("display_name")
    description = raw.get("description")
    scope_schemas_raw = raw.get("scope_schemas")
    permission_schemas_raw = raw.get("permission_schemas")
    default_permissions_raw = raw.get("default_permissions")

    if not isinstance(display_name, str) or not display_name:
        raise MalformedServicesCatalogError(f"Service '{name}' must have a non-empty display_name")
    if not isinstance(description, str):
        raise MalformedServicesCatalogError(f"Service '{name}' must have a description (string)")
    if not isinstance(scope_schemas_raw, list) or not all(isinstance(s, str) for s in scope_schemas_raw):
        raise MalformedServicesCatalogError(f"Service '{name}' scope_schemas must be a list of strings")
    if not scope_schemas_raw:
        raise MalformedServicesCatalogError(f"Service '{name}' scope_schemas must be non-empty")
    if not isinstance(permission_schemas_raw, list) or not all(isinstance(s, str) for s in permission_schemas_raw):
        raise MalformedServicesCatalogError(
            f"Service '{name}' permission_schemas must be a list of strings",
        )
    if not permission_schemas_raw:
        raise MalformedServicesCatalogError(f"Service '{name}' permission_schemas must be non-empty")

    scope_schemas: tuple[str, ...] = tuple(str(s) for s in scope_schemas_raw)
    permission_schemas: tuple[str, ...] = tuple(str(s) for s in permission_schemas_raw)

    if default_permissions_raw is None:
        default_permissions: tuple[str, ...] = _default_permissions_heuristic(permission_schemas)
    elif isinstance(default_permissions_raw, list) and all(isinstance(s, str) for s in default_permissions_raw):
        default_permissions = tuple(str(s) for s in default_permissions_raw)
    else:
        raise MalformedServicesCatalogError(
            f"Service '{name}' default_permissions must be a list of strings if specified",
        )

    # Validate that defaults are a subset of available permissions.
    missing = [perm for perm in default_permissions if perm not in permission_schemas]
    if missing:
        raise MalformedServicesCatalogError(
            f"Service '{name}' has default_permissions not in permission_schemas: {missing}",
        )

    return ServicePermissionInfo(
        name=name,
        display_name=display_name,
        description=description,
        scope_schemas=scope_schemas,
        permission_schemas=permission_schemas,
        default_permissions=default_permissions,
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
