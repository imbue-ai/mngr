"""Latchkey services catalog, fetched from the gateway's permissions extension.

The catalog tells the permission dialog the display name and the legal
set of permission schemas for a given scope (e.g. ``slack-api``).
Agents do not see this file; they emit ``{scope, permissions, rationale}``
and the dialog uses the catalog to render the same scope with a
human-readable label and a checkbox list.

The catalog is fetched from the latchkey gateway's ``/permissions/available``
endpoint (which is itself backed by the ``services.json`` data file that
ships alongside the gateway extension) and cached in-process. Callers
get a :class:`ServicesCatalog` whose first attribute access triggers the
HTTP fetch; subsequent accesses are served from the in-memory snapshot.

Defaults are not maintained per-service: every scope implicitly defaults
to the detent ``any`` schema (matches every request inside the scope), so
clicking Approve without changing anything yields ``{<scope>: ["any"]}`` --
unrestricted access for the chosen scope. The user can tighten this by
unticking ``any`` and selecting specific permissions in the dialog.
"""

import threading
from collections.abc import Mapping
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError

# The detent ``any`` schema matches every request, so a rule like
# ``{"slack-api": ["any"]}`` allows all Slack access. We prepend ``any``
# to every scope's permission list (deduplicated) so the dialog can
# render it as a checkbox, and pre-check it as the implicit default.
_IMPLICIT_DEFAULT_PERMISSION: Final[str] = "any"

IMPLICIT_DEFAULT_PERMISSIONS: Final[tuple[str, ...]] = (_IMPLICIT_DEFAULT_PERMISSION,)


class LatchkeyServicesCatalogError(Exception):
    """Base exception for catalog fetch / parse failures."""


class MalformedServicesCatalogError(LatchkeyServicesCatalogError, ValueError):
    """Raised when the gateway's catalog payload is structurally invalid."""


class ServicePermissionInfo(FrozenModel):
    """Description of a single scope's permission surface.

    The ``name`` field is the raw service name (e.g. ``slack``) used to
    key the catalog; ``scope`` is the Detent scope schema name (e.g.
    ``slack-api``) that the agent's permission request actually carries.
    """

    name: str = Field(description="Raw service name (e.g. 'slack', 'google-gmail').")
    scope: str = Field(description="Detent scope schema; matches the request event's ``scope`` field.")
    display_name: str = Field(description="Human-readable label shown in the dialog header.")
    permission_schemas: tuple[str, ...] = Field(
        description=(
            "Detent permission schemas the user can grant for this scope. The implicit "
            "``any`` default is always present at index 0."
        ),
    )


class _RawServiceEntry(FrozenModel):
    """Internal pydantic shape used to validate a single ``/permissions/available`` entry.

    Pydantic enforces the field shape (non-empty strings, list-of-strings)
    so we don't have to hand-roll isinstance checks against
    ``Mapping[Unknown, Unknown]``-shaped JSON. Validation failures are
    re-raised as :class:`MalformedServicesCatalogError`.
    """

    scope: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    permissions: tuple[str, ...] = Field(default=())


def _build_service_info(name: str, raw: object) -> ServicePermissionInfo:
    """Turn one ``{scope, display_name, permissions}`` entry into a typed info record.

    Takes ``raw`` as ``object`` (rather than ``Mapping[str, object]``)
    so the function is the sole place that asserts the shape; pydantic
    does the actual validation. Raises
    :class:`MalformedServicesCatalogError` on shape violations so a bad
    gateway response is loud rather than silently dropping the entry.
    """
    try:
        entry = _RawServiceEntry.model_validate(raw)
    except ValidationError as e:
        raise MalformedServicesCatalogError(f"Service '{name}' has a malformed entry: {e}") from e

    # Always make ``any`` available as the first checkbox, deduplicating in
    # case the gateway lists it explicitly (harmless but redundant).
    permission_schemas: tuple[str, ...] = (_IMPLICIT_DEFAULT_PERMISSION,) + tuple(
        p for p in entry.permissions if p != _IMPLICIT_DEFAULT_PERMISSION
    )

    return ServicePermissionInfo(
        name=name,
        scope=entry.scope,
        display_name=entry.display_name,
        permission_schemas=permission_schemas,
    )


def _parse_catalog_payload(payload: Mapping[str, object]) -> dict[str, ServicePermissionInfo]:
    """Validate and convert the raw ``/permissions/available`` JSON object."""
    catalog: dict[str, ServicePermissionInfo] = {}
    for service_name, raw in payload.items():
        catalog[service_name] = _build_service_info(service_name, raw)
    return catalog


class ServicesCatalog(MutableModel):
    """Lazy in-memory cache of the gateway's permission catalog.

    The first call to :meth:`get` (or :meth:`get_by_scope`, or
    :meth:`as_mapping`) issues a single
    ``GET /permissions/available`` against the gateway; subsequent calls
    are served from the cached snapshot. A fetch failure is logged and
    yields an empty catalog so dialog rendering can fall back to a
    "unknown service" page rather than crash.

    The catalog is effectively static for the lifetime of a desktop
    client process: the gateway extension is shipped as package data and
    only changes on a minds upgrade, which restarts this process. We do
    not implement TTL-based invalidation for that reason.
    """

    gateway_client: LatchkeyGatewayClient = Field(
        frozen=True,
        description="HTTP client used to fetch the catalog from the gateway.",
    )

    _by_service_name: dict[str, ServicePermissionInfo] | None = PrivateAttr(default=None)
    _by_scope: dict[str, ServicePermissionInfo] | None = PrivateAttr(default=None)
    _load_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _ensure_loaded(self) -> None:
        """Populate the cache from the gateway exactly once. Thread-safe."""
        if self._by_service_name is not None:
            return
        with self._load_lock:
            if self._by_service_name is not None:
                return
            try:
                payload = self.gateway_client.get_available_services()
            except LatchkeyGatewayClientError as e:
                logger.warning(
                    "Could not fetch latchkey services catalog from gateway; "
                    "permission dialogs will fall back to the unknown-service page: {}",
                    e,
                )
                self._by_service_name = {}
                self._by_scope = {}
                return
            try:
                parsed = _parse_catalog_payload(payload)
            except MalformedServicesCatalogError as e:
                logger.warning(
                    "Gateway returned a malformed services catalog; permission dialogs "
                    "will fall back to the unknown-service page: {}",
                    e,
                )
                self._by_service_name = {}
                self._by_scope = {}
                return
            self._by_service_name = parsed
            self._by_scope = {info.scope: info for info in parsed.values()}
            logger.debug("Loaded latchkey services catalog with {} entries from gateway", len(parsed))

    def get(self, service_name: str) -> ServicePermissionInfo | None:
        """Return the catalog entry for the raw service name, or ``None``."""
        self._ensure_loaded()
        assert self._by_service_name is not None
        return self._by_service_name.get(service_name)

    def get_by_scope(self, scope: str) -> ServicePermissionInfo | None:
        """Return the catalog entry whose ``scope`` schema matches, or ``None``.

        The permission request stream carries the scope schema (e.g.
        ``slack-api``), not the service name, so dialog rendering looks
        up the matching entry by scope.
        """
        self._ensure_loaded()
        assert self._by_scope is not None
        return self._by_scope.get(scope)

    def as_mapping(self) -> Mapping[str, ServicePermissionInfo]:
        """Return the catalog as a read-only mapping keyed by service name."""
        self._ensure_loaded()
        assert self._by_service_name is not None
        return self._by_service_name
