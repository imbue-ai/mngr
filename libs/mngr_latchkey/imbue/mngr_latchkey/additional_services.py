"""Read-only access to the bundled catalog of *additional* latchkey services.

Additional services are third-party services that are **not** part of
latchkey's builtin catalog (``services.json``, generated from detent's builtin
request schemas). minds registers each one in latchkey's ``config.json`` (the
``registeredServices`` block ``latchkey services register`` writes) and ships
its detent scope/permission schemas itself, so an agent can request and be
granted access to it exactly like a builtin service.

The data ships as ``additional_services.json`` beside this module, keyed by
canonical service name. Each value carries the human-readable ``display_name``,
the ``base_api_url`` latchkey matches requests against, the optional
``browser_login`` (the page ``latchkey auth browser`` opens plus the generic
latchkey login flow it runs there), the single Detent ``scope`` the service
exposes (its schema-name plus the inline scope schema), and the ``permissions``
grantable under it (each with an inline permission schema).

Nothing reads this file to answer "what services exist": the *catalog* entries
are folded into ``services.json`` by ``scripts/generate_services_json.py``, so
:class:`imbue.mngr_latchkey.services_catalog.ServicesCatalog` and both gateway
extensions see one file in one shape. This file remains the source of the two
things the catalog does not carry:

* the latchkey registration itself --
  :func:`additional_service_registration_entries`, consumed by
  :mod:`imbue.mngr_latchkey.core` to write the ``registeredServices`` block of
  every gateway's ``config.json`` (the desktop's and, via
  :mod:`imbue.mngr_latchkey.remote_gateway`, each VPS's).
* the inline detent schemas -- :func:`additional_service_shared_schemas` /
  :func:`shared_schemas_file_content`, materialized into the single shared file
  that every host ``permissions.json`` references via detent's ``include``, so a
  granted additional-service scope resolves without inlining schemas per host.

:func:`additional_services_catalog_payload` exists for the generator, which is
what performs the fold into ``services.json``.

The file is trusted package data copied verbatim into the wheel, so a missing
or malformed file is a packaging bug; it surfaces as
:class:`AdditionalServicesCatalogError` rather than being silently tolerated.
"""

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Annotated
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import StringConstraints
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel

# Package and filename of the bundled additional-services definitions. It sits
# beside this module rather than in ``extensions/`` because no gateway extension
# reads it -- the catalog entries it contributes reach them via ``services.json``.
_PACKAGE: Final[str] = "imbue.mngr_latchkey"
_ADDITIONAL_SERVICES_FILENAME: Final[str] = "additional_services.json"

# The service names latchkey accepts (``canonicalizeServiceName`` rejects
# anything else). We write the registration into ``config.json`` ourselves
# rather than shelling out to ``latchkey services register``, so this is where
# an unusable name in the bundled file is caught.
_SERVICE_NAME_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_-]*$"


class AdditionalServicesCatalogError(RuntimeError):
    """Raised when the bundled ``additional_services.json`` is missing or malformed.

    A standalone :class:`RuntimeError` subclass (not a ``LatchkeyError``) so
    this module stays import-light and free of a dependency on ``core``;
    callers that need a package-shaped error should catch this and re-raise.
    """


class _AdditionalServicePermissionEntry(FrozenModel):
    """One grantable permission of an additional service, as modeled from the file."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1, description="Detent permission schema name (e.g. ``everything``).")
    description: str = Field(default="", description="Plain-English summary shown in the permission dialog.")
    request_schema: Mapping[str, JsonValue] = Field(
        alias="schema", description="Inline Detent permission schema (a request matcher)."
    )


class _AdditionalServiceScopeEntry(FrozenModel):
    """The single Detent scope an additional service exposes, as modeled from the file."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1, description="Detent scope schema name; appears as a permissions rule key.")
    request_schema: Mapping[str, JsonValue] = Field(
        alias="schema", description="Inline Detent scope schema (a request matcher, matching the service domain)."
    )


class _AdditionalServiceLoginFlowEntry(FrozenModel):
    """The kind of generic browser sign-in latchkey runs, and how it is configured.

    Latchkey ships a small set of service-agnostic login flows (currently
    ``cookie-capture``) that a registered service can opt into instead of
    borrowing a builtin service's flow. ``params`` is passed through verbatim;
    latchkey validates it against the named flow's own schema, and a flow it
    cannot resolve costs the service its browser sign-in without breaking the
    rest of the registration.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, description="Latchkey login-flow name, e.g. ``cookie-capture``.")
    params: Mapping[str, JsonValue] = Field(
        default_factory=dict, description="Parameters of the flow, in the shape that flow documents."
    )


class _AdditionalServiceBrowserLoginEntry(FrozenModel):
    """How ``latchkey auth browser`` signs the user in to an additional service.

    The page to open and the flow to run it with are one field, not two
    independent ones, because latchkey silently ignores a flow with no page to
    open -- a service would come out registered but unsignable-into.
    """

    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=1, description="URL ``latchkey auth browser`` opens to sign the user in.")
    flow: _AdditionalServiceLoginFlowEntry = Field(description="The generic latchkey login flow run against ``url``.")


class _AdditionalServiceEntry(FrozenModel):
    """One additional service, as modeled from an ``additional_services.json`` value."""

    model_config = ConfigDict(extra="ignore")

    display_name: str = Field(min_length=1, description="Human-readable label shown in the permission dialog.")
    base_api_url: str = Field(min_length=1, description="Base API URL latchkey matches requests against.")
    browser_login: _AdditionalServiceBrowserLoginEntry | None = Field(
        default=None,
        description="The browser sign-in latchkey offers for this service; absent when only ``auth set`` works.",
    )
    scope: _AdditionalServiceScopeEntry = Field(description="The single Detent scope this service exposes.")
    permissions: tuple[_AdditionalServicePermissionEntry, ...] = Field(
        default=(), description="Permissions grantable under the scope, each with its plain-English summary."
    )


# The catalog is a JSON object keyed by canonical service name; a module-level
# adapter validates the bundled file, names included.
_ADDITIONAL_SERVICES_ADAPTER: Final = TypeAdapter(
    dict[Annotated[str, StringConstraints(pattern=_SERVICE_NAME_PATTERN)], _AdditionalServiceEntry]
)


@cache
def _load_additional_service_entries() -> Mapping[str, _AdditionalServiceEntry]:
    """Read and validate the bundled ``additional_services.json`` (cached once per process)."""
    resource = resources.files(_PACKAGE).joinpath(_ADDITIONAL_SERVICES_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except OSError as e:
        raise AdditionalServicesCatalogError(f"Could not read bundled {_ADDITIONAL_SERVICES_FILENAME}: {e}") from e
    try:
        return _ADDITIONAL_SERVICES_ADAPTER.validate_json(raw)
    except ValidationError as e:
        raise AdditionalServicesCatalogError(f"Bundled {_ADDITIONAL_SERVICES_FILENAME} is malformed: {e}") from e


def additional_services_catalog_payload() -> dict[str, list[dict[str, object]]]:
    """Project the additional services into the ``services.json``-shaped catalog payload.

    The result matches the shape
    :func:`imbue.mngr_latchkey.services_catalog.service_infos_from_catalog_payload`
    expects, so the dialog catalog can merge additional services alongside the
    builtin ones. Each service exposes exactly one scope, so its value is a
    single-element list.
    """
    entries = _load_additional_service_entries()
    return {
        name: [
            {
                "scope": entry.scope.name,
                "display_name": entry.display_name,
                "permissions": [
                    {"name": permission.name, "description": permission.description}
                    for permission in entry.permissions
                ],
            }
        ]
        for name, entry in entries.items()
    }


def _registration_entry(entry: _AdditionalServiceEntry) -> dict[str, JsonValue]:
    """Project one service into the ``registeredServices`` value latchkey persists.

    A service with no browser sign-in gets neither key at all rather than a
    ``null`` for each: latchkey's config schema types both as
    absent-or-a-value, and a ``null`` would make the whole entry unreadable to
    it (costing the service its registration entirely).
    """
    registration: dict[str, JsonValue] = {"baseApiUrl": entry.base_api_url}
    if entry.browser_login is not None:
        registration["loginUrl"] = entry.browser_login.url
        registration["loginFlow"] = {
            "name": entry.browser_login.flow.name,
            "params": dict(entry.browser_login.flow.params),
        }
    return registration


def additional_service_registration_entries() -> dict[str, JsonValue]:
    """Return every additional service as latchkey's ``registeredServices`` block.

    The result is keyed by canonical service name, and each value is exactly the
    object ``latchkey services register`` would persist into ``config.json``
    (see :func:`_registration_entry`). :mod:`imbue.mngr_latchkey.core` merges it
    into the config of every gateway that must inject these services'
    credentials -- the desktop's and each VPS's -- so the registration always
    travels with the credentials it belongs to.
    """
    entries = _load_additional_service_entries()
    return {name: _registration_entry(entry) for name, entry in entries.items()}


def additional_service_shared_schemas() -> dict[str, JsonValue]:
    """Return the merged Detent schemas (every scope schema + permission schema) of all additional services.

    This is the ``schemas`` map minds materializes into the single shared file
    that every host ``permissions.json`` references via detent's ``include``, so
    a granted additional-service scope resolves without inlining the schemas into
    each host file. A schema name defined by two services with *different* bodies
    is a packaging bug (the merged file is a flat namespace) and raises; an
    identical redefinition is harmless and kept.
    """
    entries = _load_additional_service_entries()
    schemas: dict[str, JsonValue] = {}
    for entry in entries.values():
        for schema_name, schema_body in (
            (entry.scope.name, entry.scope.request_schema),
            *((permission.name, permission.request_schema) for permission in entry.permissions),
        ):
            new_body: JsonValue = dict(schema_body)
            if schema_name in schemas and schemas[schema_name] != new_body:
                raise AdditionalServicesCatalogError(
                    f"Schema name {schema_name!r} is defined by more than one additional service "
                    f"with conflicting bodies; the shared schemas file is a flat namespace."
                )
            schemas[schema_name] = new_body
    return schemas


def shared_schemas_file_content() -> str:
    """Serialize the additional-service schemas as a detent config file (``{\"schemas\": {...}}``).

    The shared file carries only ``schemas`` (no rules): the grants stay in each
    per-host file, which references this file via ``include``.
    """
    return json.dumps({"schemas": additional_service_shared_schemas()}, indent=2) + "\n"
