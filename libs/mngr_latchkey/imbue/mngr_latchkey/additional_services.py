"""Read-only access to the bundled catalog of *additional* latchkey services.

Additional services are third-party services that are **not** part of
latchkey's builtin catalog (``services.json``, generated from detent's builtin
request schemas). minds registers each one with latchkey at gateway bring-up
(``latchkey services register``) and ships its detent scope/permission schemas
itself, so an agent can request and be granted access to it exactly like a
builtin service.

The data ships as ``additional_services.json`` beside this module, keyed by
canonical service name. Each value carries the human-readable ``display_name``,
the ``base_api_url`` passed to ``latchkey services register``, the single Detent
``scope`` the service exposes (its schema-name plus the inline scope schema),
and the ``permissions`` grantable under it (each with an inline permission
schema).

Nothing reads this file to answer "what services exist": the *catalog* entries
are folded into ``services.json`` by ``scripts/generate_services_json.py``, so
:class:`imbue.mngr_latchkey.services_catalog.ServicesCatalog` and both gateway
extensions see one file in one shape. This file remains the source of the two
things the catalog does not carry:

* ``base_api_url`` -- :func:`load_additional_service_registrations`, consumed by
  :mod:`imbue.mngr_latchkey.core` to register each service with the latchkey CLI
  at gateway bring-up.
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
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel

# Package and filename of the bundled additional-services definitions. It sits
# beside this module rather than in ``extensions/`` because no gateway extension
# reads it -- the catalog entries it contributes reach them via ``services.json``.
_PACKAGE: Final[str] = "imbue.mngr_latchkey"
_ADDITIONAL_SERVICES_FILENAME: Final[str] = "additional_services.json"


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


class _AdditionalServiceEntry(FrozenModel):
    """One additional service, as modeled from an ``additional_services.json`` value."""

    model_config = ConfigDict(extra="ignore")

    display_name: str = Field(min_length=1, description="Human-readable label shown in the permission dialog.")
    base_api_url: str = Field(min_length=1, description="Base API URL passed to ``latchkey services register``.")
    scope: _AdditionalServiceScopeEntry = Field(description="The single Detent scope this service exposes.")
    permissions: tuple[_AdditionalServicePermissionEntry, ...] = Field(
        default=(), description="Permissions grantable under the scope, each with its plain-English summary."
    )


class AdditionalServiceRegistration(FrozenModel):
    """A custom latchkey service minds registers with latchkey at gateway bring-up."""

    name: str = Field(description="Canonical service name passed to ``latchkey services register``.")
    base_api_url: str = Field(description="Base API URL passed to ``latchkey services register --base-api-url``.")


# The catalog is a JSON object keyed by canonical service name; a module-level
# adapter validates the bundled file.
_ADDITIONAL_SERVICES_ADAPTER: Final = TypeAdapter(dict[str, _AdditionalServiceEntry])


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


def load_additional_service_registrations() -> tuple[AdditionalServiceRegistration, ...]:
    """Return the (name, base_api_url) of every additional service to register with latchkey."""
    entries = _load_additional_service_entries()
    return tuple(
        AdditionalServiceRegistration(name=name, base_api_url=entry.base_api_url) for name, entry in entries.items()
    )


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
