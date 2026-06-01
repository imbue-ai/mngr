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

Shape validation of the gateway response lives in
:class:`LatchkeyGatewayClient.get_available_services`, which returns a
typed ``dict[str, tuple[AvailableServiceEntry, ...]]`` (each service maps
to a list of scope entries, since a single service may expose more than
one detent scope). This module only translates those typed entries into
the dialog-facing :class:`ServicePermissionInfo` records, which differ in
two ways:

* a ``name`` field carrying the raw service name (the key in the
  gateway's response), so the rest of the desktop client can pass a
  single value around instead of a ``(name, entry)`` pair;
* the catch-all ``any`` schema is injected as the first available
  permission schema. The gateway never lists it (every scope implicitly
  admits it); the dialog renders it as an opt-in checkbox, not a
  pre-checked default. See :class:`LatchkeyPermissionGrantHandler` for
  the union-based pre-check policy.
"""

import threading
from collections.abc import Mapping
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.gateway_client import AvailableServiceEntry
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError

# The detent ``any`` schema matches every request, so a rule like
# ``{"slack-api": ["any"]}`` allows all Slack access. We prepend ``any``
# to every scope's permission list (deduplicated) so the dialog can
# render it as a checkbox the user may opt into. The dialog does not
# pre-check it; see :class:`LatchkeyPermissionGrantHandler` for the
# union-based pre-check policy.
_ALWAYS_AVAILABLE_PERMISSION: Final[str] = "any"


class ServicePermissionInfo(FrozenModel):
    """Description of a single scope's permission surface.

    The ``name`` field is the raw service name (e.g. ``slack``) used to
    key the catalog; ``scope`` is the Detent scope schema name (e.g.
    ``slack-api``) that the agent's permission request actually carries.
    """

    name: str = Field(description="Raw service name (e.g. 'slack', 'google-gmail').")
    scope: str = Field(description="Detent scope schema; matches the request event's ``scope`` field.")
    display_name: str = Field(description="Human-readable label shown in the dialog header.")
    description: str = Field(
        default="",
        description="Plain-English summary of the scope (detent's ``$comment``); empty when unknown.",
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
            "Plain-English summary per permission schema name (detent's ``$comment``). "
            "Permissions without a summary are omitted; the injected ``any`` never has one."
        ),
    )


def _service_info_from_entry(name: str, entry: AvailableServiceEntry) -> ServicePermissionInfo:
    """Translate a gateway-validated :class:`AvailableServiceEntry` into a dialog-facing record.

    Prepends the catch-all ``any`` schema as the first available option,
    deduplicating in case the gateway lists it explicitly (harmless but
    redundant). The dialog renders it as an opt-in choice, not a
    pre-checked default. Detent's per-schema descriptions are carried
    over so the dialog can show them next to each permission.
    """
    permission_schemas: tuple[str, ...] = (_ALWAYS_AVAILABLE_PERMISSION,) + tuple(
        permission.name for permission in entry.permissions if permission.name != _ALWAYS_AVAILABLE_PERMISSION
    )
    description_by_permission_name = {
        permission.name: permission.description for permission in entry.permissions if len(permission.description) > 0
    }
    return ServicePermissionInfo(
        name=name,
        scope=entry.scope,
        display_name=entry.display_name,
        description=entry.description,
        permission_schemas=permission_schemas,
        description_by_permission_name=description_by_permission_name,
    )


class ServicesCatalog(MutableModel):
    """Lazy in-memory cache of the gateway's permission catalog.

    The first call to :meth:`get` (or :meth:`get_by_scope`, or
    :meth:`as_mapping`) issues a single
    ``GET /permissions/available`` against the gateway; subsequent calls
    are served from the cached snapshot. A fetch failure is logged and
    yields an empty catalog so dialog rendering can fall back to a
    "unknown service" page rather than crash. The gateway client is
    responsible for validating the wire payload, so anything the
    catalog receives is already typed and well-formed.

    The catalog is effectively static for the lifetime of a desktop
    client process: the gateway extension is shipped as package data and
    only changes on a minds upgrade, which restarts this process. We do
    not implement TTL-based invalidation for that reason.
    """

    gateway_client: LatchkeyGatewayClient = Field(
        frozen=True,
        description="HTTP client used to fetch the catalog from the gateway.",
    )

    _by_service_name: dict[str, tuple[ServicePermissionInfo, ...]] | None = PrivateAttr(default=None)
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
                entries = self.gateway_client.get_available_services()
            except LatchkeyGatewayClientError as e:
                # Both transport-level failures and per-entry validation
                # errors surface as ``LatchkeyGatewayClientError`` from
                # the client; the catalog treats every one of them as
                # "no catalog" and lets the unknown-scope page render.
                logger.warning(
                    "Could not fetch latchkey services catalog from gateway; "
                    "permission dialogs will fall back to the unknown-service page: {}",
                    e,
                )
                self._by_service_name = {}
                self._by_scope = {}
                return
            parsed = {
                name: tuple(_service_info_from_entry(name, entry) for entry in service_entries)
                for name, service_entries in entries.items()
            }
            self._by_service_name = parsed
            self._by_scope = {info.scope: info for infos in parsed.values() for info in infos}
            logger.debug("Loaded latchkey services catalog with {} services from gateway", len(parsed))

    def get(self, service_name: str) -> tuple[ServicePermissionInfo, ...]:
        """Return the catalog entries for the raw service name.

        A service may expose more than one detent scope, so this returns
        a tuple of :class:`ServicePermissionInfo` (one per scope). An
        unknown service yields the empty tuple.
        """
        self._ensure_loaded()
        assert self._by_service_name is not None
        return self._by_service_name.get(service_name, ())

    def get_by_scope(self, scope: str) -> ServicePermissionInfo | None:
        """Return the catalog entry whose ``scope`` schema matches, or ``None``.

        The permission request stream carries the scope schema (e.g.
        ``slack-api``), not the service name, so dialog rendering looks
        up the matching entry by scope.
        """
        self._ensure_loaded()
        assert self._by_scope is not None
        return self._by_scope.get(scope)

    def as_mapping(self) -> Mapping[str, tuple[ServicePermissionInfo, ...]]:
        """Return the catalog as a read-only mapping keyed by service name.

        Each value is the tuple of :class:`ServicePermissionInfo` records
        for that service (one per detent scope it exposes).
        """
        self._ensure_loaded()
        assert self._by_service_name is not None
        return self._by_service_name
