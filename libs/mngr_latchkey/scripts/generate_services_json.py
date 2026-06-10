"""Regenerate the latchkey ``services.json`` permission catalog from detent.

This standalone developer tool reads detent's built-in request schemas
(``src/schemas/builtin/*.json`` in a detent checkout) and rewrites the
``services.json`` catalog that ships alongside the latchkey ``permissions``
gateway extension.

Each detent built-in file describes one service (the file name is the raw
service name, e.g. ``slack.json`` -> ``slack``). Within a file, every
top-level schema is either a *scope* (matches a whole service, used as a
detent rule key) or a *permission* (a narrower grant, used as a rule value).
The scope/permission classification mirrors detent's own
``scripts/generateBuiltinSchemaDocs.ts``: a scope requires ``domain`` and does
not constrain ``method``; everything else is a permission. AWS is special-cased
exactly as detent does (only the top-level ``aws`` schema is a scope; the
service-specific ``aws-s3`` etc. double as permissions inside it).

detent's recent ``$comment`` annotations on each schema are carried over into
the catalog under the friendlier ``description`` key: the scope's summary sits
on the scope entry, and each permission becomes a ``{"name", "description"}``
object so its summary is colocated with its name.

Display names and the service ordering are editorial metadata that detent does
not carry, so they live here as curated constants. A scope without a curated
display name falls back to a title-cased service name and is reported on stderr
so a maintainer can curate it.

Run with::

    uv run python libs/mngr_latchkey/scripts/generate_services_json.py \
        --detent-root /path/to/detent
"""

import argparse
import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging

# detent built-in schemas live under this subdirectory of a detent checkout.
_BUILTIN_SCHEMAS_SUBPATH: Final[str] = "src/schemas/builtin"

# The catalog ships next to the permissions gateway extension.
_DEFAULT_OUTPUT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "imbue" / "mngr_latchkey" / "extensions" / "services.json"
)

# The detent ``$comment`` field name we copy descriptions out of.
_COMMENT_KEY: Final[str] = "$comment"

# The ``any.json`` catch-all is not a service and has no scope; skip it.
_NON_SERVICE_FILES: Final[frozenset[str]] = frozenset({"any.json"})

# AWS is structurally ambiguous: every ``aws-*`` schema matches only on domain
# and so looks like a scope, but detent treats only the top-level ``aws`` schema
# as a scope and folds the service-specific ones in as permissions.
_AWS_SCHEMA_FILE: Final[str] = "aws.json"
_AWS_SCOPE_SCHEMAS: Final[frozenset[str]] = frozenset({"aws"})

# Human-readable scope labels. detent has no notion of a display name, so this
# is curated here. Keyed by detent scope schema name.
_DISPLAY_NAME_BY_SCOPE: Final[Mapping[str, str]] = {
    "slack-api": "Slack",
    "discord-api": "Discord",
    "github-rest-api": "GitHub (REST API)",
    "github-git": "GitHub (git)",
    "gitlab-api": "GitLab (REST API)",
    "gitlab-git": "GitLab (git)",
    "dropbox-api": "Dropbox",
    "linear-api": "Linear",
    "notion-api": "Notion",
    "notion-mcp-api": "Notion (MCP)",
    "mailchimp-api": "Mailchimp",
    "zoom-api": "Zoom",
    "telegram-api": "Telegram",
    "sentry-api": "Sentry",
    "aws": "AWS",
    "stripe-api": "Stripe",
    "figma-api": "Figma",
    "calendly-api": "Calendly",
    "yelp-api": "Yelp",
    "coolify-api": "Coolify",
    "umami-api": "Umami",
    "google-gmail-api": "Gmail",
    "google-calendar-api": "Google Calendar",
    "google-drive-api": "Google Drive",
    "google-docs-api": "Google Docs",
    "google-sheets-api": "Google Sheets",
    "google-people-api": "Google Contacts",
    "google-analytics-api": "Google Analytics",
    "google-directions-api": "Google Directions",
}

# Curated order in which services appear in the catalog (and thus in the
# permission dialog). Services not listed here are appended alphabetically.
_SERVICE_ORDER: Final[Sequence[str]] = (
    "slack",
    "discord",
    "github",
    "gitlab",
    "dropbox",
    "linear",
    "notion",
    "notion-mcp",
    "mailchimp",
    "zoom",
    "telegram",
    "sentry",
    "aws",
    "stripe",
    "figma",
    "calendly",
    "yelp",
    "coolify",
    "umami",
    "google-gmail",
    "google-calendar",
    "google-drive",
    "google-docs",
    "google-sheets",
    "google-people",
    "google-analytics",
    "google-directions",
)


class GenerateServicesError(Exception):
    """Base error for the services.json generator."""

    ...


class DetentSchemasNotFoundError(GenerateServicesError, FileNotFoundError):
    """Raised when the detent built-in schema directory cannot be located."""

    ...


class _CatalogPermission(FrozenModel):
    """A single grantable permission schema and its plain-English summary."""

    name: str = Field(description="Detent permission schema name (e.g. ``slack-read-all``).")
    description: str = Field(description="Plain-English summary of the permission (detent's ``$comment``).")


class _ScopeCatalogEntry(FrozenModel):
    """One scope a service exposes, plus the permissions grantable under it."""

    scope: str = Field(description="Detent scope schema name (e.g. ``slack-api``).")
    display_name: str = Field(description="Human-readable label shown in the permission dialog.")
    description: str = Field(description="Plain-English summary of the scope (detent's ``$comment``).")
    permissions: tuple[_CatalogPermission, ...] = Field(
        description="Permissions grantable under the scope, each with its plain-English summary.",
    )


def _is_scope_schema(schema_name: str, schema: Mapping[str, object], file_name: str) -> bool:
    """Whether a detent schema identifies a whole service (a scope) vs. a narrower permission."""
    if file_name == _AWS_SCHEMA_FILE:
        return schema_name in _AWS_SCOPE_SCHEMAS
    required = schema.get("required")
    required_fields = required if isinstance(required, list) else []
    properties = schema.get("properties")
    property_names = properties if isinstance(properties, dict) else {}
    return "domain" in required_fields and "method" not in property_names


def _select_scope_for_permission(
    permission_name: str,
    scopes_in_order: Sequence[str],
) -> str:
    """Pick the scope a permission belongs to: the longest scope name that prefixes it.

    A permission ``github-git-read`` belongs to scope ``github-git`` (the
    longest scope whose name prefixes it). Permissions that match no scope name
    (e.g. ``github-read-all`` under ``github-rest-api``) fall back to the first
    scope declared in the file, which is the service's primary scope.
    """
    matching_scopes = [
        scope for scope in scopes_in_order if permission_name == scope or permission_name.startswith(f"{scope}-")
    ]
    if matching_scopes:
        return max(matching_scopes, key=lambda scope: len(scope))
    return scopes_in_order[0]


def _display_name_for_scope(scope_name: str, service_name: str) -> str:
    """Return the curated display name for a scope, or a title-cased fallback."""
    curated_name = _DISPLAY_NAME_BY_SCOPE.get(scope_name)
    if curated_name is not None:
        return curated_name
    fallback_name = service_name.replace("-", " ").title()
    logger.warning(
        "Used fallback display name {!r} for uncurated scope {!r}; "
        "add it to _DISPLAY_NAME_BY_SCOPE in generate_services_json.py",
        fallback_name,
        scope_name,
    )
    return fallback_name


def _description_for_schema(schema: Mapping[str, object]) -> str:
    """Return a schema's ``$comment`` annotation, or an empty string when absent."""
    comment = schema.get(_COMMENT_KEY)
    return comment if isinstance(comment, str) else ""


def _build_scope_entries_for_service(
    service_name: str,
    schemas_by_name: Mapping[str, Mapping[str, object]],
) -> list[_ScopeCatalogEntry]:
    """Build the ordered scope entries for a single detent service file."""
    file_name = f"{service_name}.json"
    scopes_in_order = [name for name, schema in schemas_by_name.items() if _is_scope_schema(name, schema, file_name)]
    if len(scopes_in_order) == 0:
        return []

    # Group each permission under its owning scope, preserving file order.
    permission_names_by_scope: dict[str, list[str]] = {scope: [] for scope in scopes_in_order}
    for schema_name in schemas_by_name:
        if schema_name in permission_names_by_scope:
            continue
        owning_scope = _select_scope_for_permission(schema_name, scopes_in_order)
        permission_names_by_scope[owning_scope].append(schema_name)

    # Assemble one catalog entry per scope, colocating each permission's
    # description with its name and the scope's description on the entry.
    entries: list[_ScopeCatalogEntry] = []
    for scope_name in scopes_in_order:
        permission_names = permission_names_by_scope[scope_name]
        permissions = tuple(
            _CatalogPermission(
                name=permission_name,
                description=_description_for_schema(schemas_by_name[permission_name]),
            )
            for permission_name in permission_names
        )
        entries.append(
            _ScopeCatalogEntry(
                scope=scope_name,
                display_name=_display_name_for_scope(scope_name, service_name),
                description=_description_for_schema(schemas_by_name[scope_name]),
                permissions=permissions,
            )
        )
    return entries


def _read_service_schema_file(file_path: Path) -> dict[str, Mapping[str, object]]:
    """Load and minimally validate a detent built-in schema file."""
    try:
        raw_text = file_path.read_text()
    except OSError as e:
        raise GenerateServicesError(f"Cannot read detent schema file: {file_path}") from e
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise GenerateServicesError(f"Invalid JSON in detent schema file: {file_path}") from e
    if not isinstance(parsed, dict):
        raise GenerateServicesError(f"Detent schema file is not a JSON object: {file_path}")
    return parsed


def _service_sort_key(service_name: str) -> tuple[int, str]:
    """Sort key placing curated services first (in curated order), others alphabetically after."""
    try:
        return (_SERVICE_ORDER.index(service_name), "")
    except ValueError:
        return (len(_SERVICE_ORDER), service_name)


def build_services_catalog(builtin_schemas_directory: Path) -> dict[str, list[dict[str, object]]]:
    """Build the full services.json catalog from a detent built-in schema directory."""
    if not builtin_schemas_directory.is_dir():
        raise DetentSchemasNotFoundError(f"Detent built-in schema directory not found: {builtin_schemas_directory}")

    # Collect scope entries for every service file that defines at least one scope.
    entries_by_service_name: dict[str, list[_ScopeCatalogEntry]] = {}
    for file_path in sorted(builtin_schemas_directory.glob("*.json")):
        if file_path.name in _NON_SERVICE_FILES:
            continue
        service_name = file_path.stem
        schemas_by_name = _read_service_schema_file(file_path)
        scope_entries = _build_scope_entries_for_service(service_name, schemas_by_name)
        if len(scope_entries) > 0:
            entries_by_service_name[service_name] = scope_entries

    # Emit services in curated order, serializing each entry to a plain dict.
    ordered_service_names = sorted(entries_by_service_name, key=_service_sort_key)
    return {
        service_name: [entry.model_dump() for entry in entries_by_service_name[service_name]]
        for service_name in ordered_service_names
    }


def _write_catalog(catalog: Mapping[str, object], output_path: Path) -> None:
    """Write the catalog as pretty-printed JSON with a trailing newline."""
    output_path.write_text(json.dumps(catalog, indent=2) + "\n")


def main() -> None:
    setup_logging(level="INFO")
    parser = argparse.ArgumentParser(description="Regenerate latchkey services.json from detent's schemas.")
    parser.add_argument(
        "--detent-root",
        type=Path,
        required=True,
        help="Path to a detent checkout (the directory containing src/schemas/builtin).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT_PATH,
        help="Where to write services.json (defaults to the bundled extension copy).",
    )
    arguments = parser.parse_args()

    builtin_schemas_directory = arguments.detent_root / _BUILTIN_SCHEMAS_SUBPATH
    catalog = build_services_catalog(builtin_schemas_directory)
    _write_catalog(catalog, arguments.output)
    logger.info(
        "Wrote {} services to {}",
        len(catalog),
        arguments.output,
    )


if __name__ == "__main__":
    main()
