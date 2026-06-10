"""Read-only access to the bundled latchkey service catalog (``services.json``).

``services.json`` ships beside the gateway permissions extension
(``imbue.mngr_latchkey.extensions``) and is the same file the
``permissions.mjs`` extension serves from ``GET /permissions/available``.
It is a JSON object keyed by *raw* canonical service name (``slack``,
``github``, ``google-gmail``, ...). Each value is a list of scope
entries, each with a ``scope`` field naming the Detent scope schema --
the very string that appears as a rule key in a per-host
``permissions.json`` (``{"slack-api": [...]}``). A single service may
expose more than one scope (e.g. ``github`` -> ``github-rest-api``,
``github-git``).

This module is the single chokepoint for reading that file: nothing
else should touch ``services.json`` directly. It inverts the catalog
into a ``scope schema name -> canonical service name`` index so callers
can map the scopes a host has been granted back to the canonical
service names whose credentials should be shipped to that host.

The file is trusted package data copied verbatim into the wheel, so a
missing or malformed file is a packaging bug; it surfaces as
:class:`ServiceCatalogError` rather than being silently tolerated.
"""

from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

# Package and filename of the bundled catalog. Kept in sync with the copy
# ``core._materialize_bundled_extensions`` ships into the gateway's
# ``LATCHKEY_DIRECTORY/extensions`` directory at spawn time -- both read
# the same source file out of this package.
_EXTENSIONS_PACKAGE: Final[str] = "imbue.mngr_latchkey.extensions"
_SERVICES_CATALOG_FILENAME: Final[str] = "services.json"

# Detent's wildcard scope key. A rule keyed ``any`` (e.g. the admin
# ``{"any": ["any"]}`` grant) authorizes every service, so a host
# carrying it resolves to the full catalog rather than a finite subset.
_WILDCARD_SCOPE: Final[str] = "any"


class ServiceCatalogError(RuntimeError):
    """Raised when the bundled ``services.json`` is missing or malformed.

    A standalone :class:`RuntimeError` subclass (not a ``LatchkeyError``)
    so this module stays import-light and free of a dependency on
    ``core``; callers that need a package-shaped error should catch this
    and re-raise.
    """


class _ServiceScopeEntry(FrozenModel):
    """One scope a service exposes. Only ``scope`` is consumed here.

    ``services.json`` entries also carry ``display_name``,
    ``description``, and ``permissions`` (served by the ``permissions.mjs``
    extension's ``/permissions/available`` endpoint); ``extra="ignore"``
    drops them since the scope->service mapping is all this module needs.
    """

    model_config = ConfigDict(extra="ignore")

    scope: str = Field(min_length=1, description="Detent scope schema name, as it appears as a permissions rule key")


# The catalog is a JSON object keyed by canonical service name, each value
# a list of scope entries. A module-level adapter validates it on load.
_CATALOG_ADAPTER: Final = TypeAdapter(dict[str, list[_ServiceScopeEntry]])


@cache
def _load_catalog() -> Mapping[str, list[_ServiceScopeEntry]]:
    """Parse, validate, and return the bundled ``services.json``.

    Cached: the file is immutable package data, so it is read and parsed
    at most once per process. Raises :class:`ServiceCatalogError` if the
    file cannot be read or does not match the expected schema (a corrupt
    bundled catalog is a packaging bug).
    """
    resource = resources.files(_EXTENSIONS_PACKAGE).joinpath(_SERVICES_CATALOG_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except OSError as e:
        raise ServiceCatalogError(f"Could not read bundled {_SERVICES_CATALOG_FILENAME}: {e}") from e
    try:
        return _CATALOG_ADAPTER.validate_json(raw)
    except ValidationError as e:
        raise ServiceCatalogError(f"Bundled {_SERVICES_CATALOG_FILENAME} is malformed: {e}") from e


@cache
def all_service_names() -> frozenset[str]:
    """Return every canonical service name present in the catalog."""
    return frozenset(_load_catalog().keys())


@cache
def _scope_to_service_index() -> Mapping[str, str]:
    """Return the inverse ``scope schema name -> canonical service name`` index."""
    return {entry.scope: service_name for service_name, entries in _load_catalog().items() for entry in entries}


def services_for_permissions(config: LatchkeyPermissionsConfig) -> frozenset[str]:
    """Resolve the canonical service names a permissions config grants access to.

    Each rule in ``config.rules`` is a single-key ``{scope: [permission,
    ...]}`` object; the key is a Detent scope schema name. This maps each
    such scope back to its canonical service name via the bundled
    catalog. Scopes that are not third-party services -- minds' own
    internal scopes (``minds-api-proxy-unauthorized``, the gateway-self
    schemas, ...) -- are simply absent from the catalog and dropped, so
    they contribute no service. The Detent wildcard scope (``any``)
    grants every service and therefore resolves to the full catalog.

    Returns an empty set for a deny-all config (no rules), which is the
    safe default: a host with no grants has no credentials shipped to it.
    """
    scope_keys = [next(iter(rule)) for rule in config.rules if len(rule) == 1]
    if _WILDCARD_SCOPE in scope_keys:
        return all_service_names()
    index = _scope_to_service_index()
    return frozenset(index[scope] for scope in scope_keys if scope in index)
