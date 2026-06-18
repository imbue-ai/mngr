"""Read-only access to the bundled latchkey service catalog (``services.json``).

``services.json`` ships beside the gateway permissions extension
(``imbue.mngr_latchkey.extensions``). It is a JSON object keyed by *raw*
canonical service name (``slack``, ``github``, ``google-gmail``, ...).
Each value is a list of scope entries, each with a ``scope`` field naming
the Detent scope schema -- the very string that appears as a rule key in
a per-host ``permissions.json`` (``{"slack-api": [...]}``) and that an
agent's permission request carries -- plus a human-readable
``display_name``, an optional ``description`` (Detent's ``$comment``),
and the grantable ``permissions`` (each with its own optional
description). A single service may expose more than one scope (e.g.
``github`` -> ``github-rest-api``, ``github-git``).

This module is the single chokepoint for that file. All access goes
through :class:`ServicesCatalog`, which serves two layers:

* The credential-sync path (``remote_gateway``) uses
  :meth:`ServicesCatalog.services_for_permissions` /
  :meth:`ServicesCatalog.all_service_names` to map the scopes a host has
  been granted back to the canonical service names whose credentials
  should be shipped to that host.
* The desktop permission dialog uses :meth:`ServicesCatalog.get` /
  :meth:`ServicesCatalog.get_by_scope` / :meth:`ServicesCatalog.as_mapping`
  (returning :class:`ServicePermissionInfo`) to render a granted scope
  with its display name and the checkbox list of grantable permissions.

The dialog used to fetch this from the running gateway's
``GET /permissions/available`` endpoint, but that endpoint was a pure
pass-through of this same file, so the catalog now reads the bundled file
directly -- no gateway, no network, no liveness coupling.

The file is trusted package data copied verbatim into the wheel, so a
missing or malformed file is a packaging bug; it surfaces as
:class:`ServiceCatalogError` rather than being silently tolerated.
"""

import threading
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

# Package and filename of the bundled catalog. Kept in sync with the copy
# ``core._materialize_bundled_extensions`` ships into the gateway's
# ``LATCHKEY_DIRECTORY/extensions`` directory at spawn time -- both read
# the same source file out of this package.
_EXTENSIONS_PACKAGE: Final[str] = "imbue.mngr_latchkey.extensions"
_SERVICES_CATALOG_FILENAME: Final[str] = "services.json"

# Detent's wildcard *scope* key. A rule keyed ``any`` (e.g. the admin
# ``{"any": ["any"]}`` grant) authorizes every service, so a host
# carrying it resolves to the full catalog rather than a finite subset.
_WILDCARD_SCOPE: Final[str] = "any"

# Detent's catch-all *permission* schema. It matches every request, so a
# rule like ``{"slack-api": ["any"]}`` grants all Slack access. The
# catalog file never lists it (every scope implicitly admits it); the
# dialog injects it as an opt-in, never-pre-checked option. It is the
# stored/wire value; the dialog presents it to users as ``all`` (see
# the handlers' template layer) for clarity.
WILDCARD_PERMISSION_NAME: Final[str] = "any"


class ServiceCatalogError(RuntimeError):
    """Raised when the bundled ``services.json`` is missing or malformed.

    A standalone :class:`RuntimeError` subclass (not a ``LatchkeyError``)
    so this module stays import-light and free of a dependency on
    ``core``; callers that need a package-shaped error should catch this
    and re-raise.
    """


class _AvailablePermission(FrozenModel):
    """A single grantable permission schema and its plain-English summary."""

    name: str = Field(min_length=1, description="Detent permission schema name (e.g. ``slack-read-all``).")
    description: str = Field(
        default="", description="Plain-English summary of the permission (Detent's ``$comment``)."
    )


class _ServiceScopeEntry(FrozenModel):
    """One scope a service exposes, as modeled from a ``services.json`` entry.

    ``extra="ignore"`` tolerates forward-compatible fields the file might
    grow without breaking the load.
    """

    model_config = ConfigDict(extra="ignore")

    scope: str = Field(min_length=1, description="Detent scope schema name; appears as a permissions rule key.")
    display_name: str = Field(min_length=1, description="Human-readable label shown in the dialog header.")
    description: str = Field(default="", description="Plain-English summary of the scope (Detent's ``$comment``).")
    permissions: tuple[_AvailablePermission, ...] = Field(
        default=(), description="Permissions the user can grant for this scope, each with its summary."
    )


class ServicePermissionInfo(FrozenModel):
    """Dialog-facing description of a single scope's permission surface.

    ``name`` is the raw service name (e.g. ``slack``); ``scope`` is the
    Detent scope schema (e.g. ``slack-api``) that an agent's permission
    request actually carries.
    """

    name: str = Field(description="Raw service name (e.g. 'slack', 'google-gmail').")
    scope: str = Field(description="Detent scope schema; matches the request event's ``scope`` field.")
    display_name: str = Field(description="Human-readable label shown in the dialog header.")
    description: str = Field(
        default="", description="Plain-English summary of the scope (Detent's ``$comment``); empty when unknown."
    )
    permission_schemas: tuple[str, ...] = Field(
        description=(
            "Detent permission schemas the user can grant for this scope. The catch-all "
            "``any`` schema is always injected at index 0 as an available option (not "
            "pre-checked) so the user can opt into unrestricted access if they want."
        ),
    )
    description_by_permission_name: Mapping[str, str] = Field(
        default_factory=dict,
        description=(
            "Plain-English summary per permission schema name (Detent's ``$comment``). "
            "Permissions without a summary are omitted; the injected ``any`` never has one."
        ),
    )


# The catalog is a JSON object keyed by canonical service name, each value
# a list of scope entries. A module-level adapter validates both the
# bundled file and any in-memory payload tests inject.
_CATALOG_ADAPTER: Final = TypeAdapter(dict[str, list[_ServiceScopeEntry]])


def _service_info_from_entry(name: str, entry: _ServiceScopeEntry) -> ServicePermissionInfo:
    """Translate a validated scope entry into a dialog-facing record.

    Prepends the catch-all ``any`` schema as the first available option,
    deduplicating in case the file lists it explicitly. The dialog
    renders it as an opt-in choice, not a pre-checked default. Per-schema
    descriptions are carried over so the dialog can show them.
    """
    permission_schemas: tuple[str, ...] = (WILDCARD_PERMISSION_NAME,) + tuple(
        permission.name for permission in entry.permissions if permission.name != WILDCARD_PERMISSION_NAME
    )
    description_by_permission_name = {
        permission.name: permission.description for permission in entry.permissions if permission.description
    }
    return ServicePermissionInfo(
        name=name,
        scope=entry.scope,
        display_name=entry.display_name,
        description=entry.description,
        permission_schemas=permission_schemas,
        description_by_permission_name=description_by_permission_name,
    )


def _build_catalog(validated: Mapping[str, list[_ServiceScopeEntry]]) -> dict[str, tuple[ServicePermissionInfo, ...]]:
    """Translate validated scope entries into dialog-facing records keyed by service name."""
    return {
        name: tuple(_service_info_from_entry(name, entry) for entry in entries) for name, entries in validated.items()
    }


def service_infos_from_catalog_payload(
    payload: Mapping[str, object],
) -> dict[str, tuple[ServicePermissionInfo, ...]]:
    """Validate a raw ``services.json``-shaped payload into dialog-facing records.

    Intended for tests that want a controlled catalog without depending
    on the shipped file. Raises :class:`ServiceCatalogError` if the
    payload does not match the catalog schema.
    """
    try:
        validated = _CATALOG_ADAPTER.validate_python(dict(payload))
    except ValidationError as e:
        raise ServiceCatalogError(f"Catalog payload is malformed: {e}") from e
    return _build_catalog(validated)


@cache
def _load_bundled_catalog() -> Mapping[str, tuple[ServicePermissionInfo, ...]]:
    """Read, validate, and translate the bundled ``services.json`` (cached once per process)."""
    resource = resources.files(_EXTENSIONS_PACKAGE).joinpath(_SERVICES_CATALOG_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except OSError as e:
        raise ServiceCatalogError(f"Could not read bundled {_SERVICES_CATALOG_FILENAME}: {e}") from e
    try:
        validated = _CATALOG_ADAPTER.validate_json(raw)
    except ValidationError as e:
        raise ServiceCatalogError(f"Bundled {_SERVICES_CATALOG_FILENAME} is malformed: {e}") from e
    catalog = _build_catalog(validated)
    logger.debug("Loaded latchkey services catalog with {} service(s) from bundled file", len(catalog))
    return catalog


class ServicesCatalog(MutableModel):
    """In-memory snapshot of the service catalog, the single access point for the data.

    Both consumers go through this class: the desktop permission dialog
    (:meth:`get` / :meth:`get_by_scope` / :meth:`as_mapping`) and the
    credential-sync path (:meth:`services_for_permissions` /
    :meth:`all_service_names`).

    Production constructs ``ServicesCatalog()`` and the bundled
    ``services.json`` is read lazily on first access (and memoized across
    instances via the module-level cache). Tests pass an explicit
    ``catalog_override`` -- typically via :meth:`from_catalog_payload` --
    to avoid depending on the shipped file.

    Unlike the previous gateway-backed implementation, there is no fetch
    that can fail at runtime: the catalog is local package data, so a
    load failure is a packaging bug that surfaces as
    :class:`ServiceCatalogError`.
    """

    catalog_override: Mapping[str, tuple[ServicePermissionInfo, ...]] | None = Field(
        default=None,
        description="Explicit catalog for tests; when None, the bundled services.json is read lazily.",
    )

    _by_service_name: dict[str, tuple[ServicePermissionInfo, ...]] | None = PrivateAttr(default=None)
    _by_scope: dict[str, ServicePermissionInfo] | None = PrivateAttr(default=None)
    _load_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _ensure_loaded(self) -> None:
        """Populate the in-memory indexes once. Thread-safe."""
        if self._by_service_name is not None:
            return
        with self._load_lock:
            if self._by_service_name is not None:
                return
            catalog = dict(self.catalog_override if self.catalog_override is not None else _load_bundled_catalog())
            self._by_service_name = catalog
            self._by_scope = {info.scope: info for infos in catalog.values() for info in infos}

    def get(self, service_name: str) -> tuple[ServicePermissionInfo, ...]:
        """Return the catalog entries for the raw service name (empty tuple if unknown).

        A service may expose more than one Detent scope, so this returns
        one :class:`ServicePermissionInfo` per scope.
        """
        self._ensure_loaded()
        assert self._by_service_name is not None
        return self._by_service_name.get(service_name, ())

    def get_by_scope(self, scope: str) -> ServicePermissionInfo | None:
        """Return the catalog entry whose ``scope`` schema matches, or ``None``.

        The permission request stream carries the scope schema (e.g.
        ``slack-api``), not the service name, so dialog rendering looks up
        the matching entry by scope.
        """
        self._ensure_loaded()
        assert self._by_scope is not None
        return self._by_scope.get(scope)

    def as_mapping(self) -> Mapping[str, tuple[ServicePermissionInfo, ...]]:
        """Return the catalog as a read-only mapping keyed by service name."""
        self._ensure_loaded()
        assert self._by_service_name is not None
        return self._by_service_name

    def all_service_names(self) -> frozenset[str]:
        """Return every canonical service name present in the catalog."""
        self._ensure_loaded()
        assert self._by_service_name is not None
        return frozenset(self._by_service_name.keys())

    def services_for_permissions(self, config: LatchkeyPermissionsConfig) -> frozenset[str]:
        """Resolve the canonical service names a permissions config grants access to.

        Each rule in ``config.rules`` is a single-key ``{scope: [permission,
        ...]}`` object; the key is a Detent scope schema name. This maps each
        such scope back to its canonical service name via the catalog. Scopes
        that are not third-party services -- minds' own internal scopes
        (``minds-api-proxy-unauthorized``, the gateway-self schemas, ...) --
        are simply absent from the catalog and dropped, so they contribute no
        service. The Detent wildcard scope (``any``) grants every service and
        therefore resolves to the full catalog.

        Returns an empty set for a deny-all config (no rules), which is the
        safe default: a host with no grants has no credentials shipped to it.
        """
        self._ensure_loaded()
        assert self._by_scope is not None
        scope_keys = [next(iter(rule)) for rule in config.rules if len(rule) == 1]
        if _WILDCARD_SCOPE in scope_keys:
            return self.all_service_names()
        return frozenset(self._by_scope[scope].name for scope in scope_keys if scope in self._by_scope)

    @classmethod
    def from_catalog_payload(cls, payload: Mapping[str, object]) -> "ServicesCatalog":
        """Build a catalog from a raw ``services.json``-shaped payload (for tests)."""
        return cls(catalog_override=service_infos_from_catalog_payload(payload))
