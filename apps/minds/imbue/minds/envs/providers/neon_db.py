"""Create / delete Neon databases via the Neon REST API.

Each dynamic dev env gets its own Neon database, named ``minds-dev-<name>``,
under a shared dev-tier Neon project. Authentication uses an API token the
dev tier owns in Vault (``secrets/minds/dev/neon`` plus the
operator-side Neon project id from ``deploy.toml``).

Returns the pooled connection string the connector / connector clients use
at runtime.
"""

from typing import Final

import httpx
from pydantic import Field
from pydantic import SecretStr
from pydantic import TypeAdapter

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

_NEON_API_BASE: Final[str] = "https://console.neon.tech/api/v2"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0


class NeonProviderError(MindError):
    """Raised when the Neon API rejects a request."""


class NeonBranchSummary(FrozenModel):
    """One row of ``GET /projects/{id}/branches``."""

    id: str
    name: str


class NeonDatabaseRecord(FrozenModel):
    """Result of ``create_neon_database``."""

    project_id: str = Field(description="Neon project id this DB lives under (dev-tier shared project).")
    branch_id: str = Field(description="Neon branch id (typically the main branch).")
    database_name: str = Field(description="The created database name.")
    role_name: str = Field(description="The role minds owns on this DB.")
    pooled_dsn: SecretStr = Field(description="Pooled PostgreSQL connection string.")


_BRANCH_LIST_ADAPTER: TypeAdapter[list[NeonBranchSummary]] = TypeAdapter(list[NeonBranchSummary])


def _neon_request(
    method: str,
    path: str,
    *,
    api_token: SecretStr,
    json_body: dict | None = None,
) -> dict:
    headers = {
        "Authorization": f"Bearer {api_token.get_secret_value()}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.request(method, f"{_NEON_API_BASE}{path}", headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise NeonProviderError(f"Neon API request failed ({method} {path}): {exc}") from exc
    if response.status_code >= 400:
        raise NeonProviderError(f"Neon API returned {response.status_code} for {method} {path}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise NeonProviderError(f"Neon API returned non-JSON for {method} {path}: {exc}") from exc


def _resolve_default_branch(project_id: str, *, api_token: SecretStr) -> NeonBranchSummary:
    payload = _neon_request("GET", f"/projects/{project_id}/branches", api_token=api_token)
    branches_raw = payload.get("branches")
    if not isinstance(branches_raw, list) or not branches_raw:
        raise NeonProviderError(f"Neon project {project_id} has no branches")
    branches = _BRANCH_LIST_ADAPTER.validate_python(branches_raw)
    for branch in branches:
        if branch.name == "main":
            return branch
    return branches[0]


def create_neon_database(
    name: DevEnvName,
    *,
    project_id: str,
    api_token: SecretStr,
    role_name: str = "minds_dev",
) -> NeonDatabaseRecord:
    """Create a database named ``minds-dev-<name>`` on the dev-tier project.

    Ensures ``role_name`` exists on the project (creating it if not) so the
    new database has a non-superuser owner. Returns the pooled DSN for the
    new database; not idempotent (Neon rejects duplicate database names
    with HTTP 409).
    """
    branch = _resolve_default_branch(project_id, api_token=api_token)
    database_name = f"minds-dev-{name}"

    # Neon returns 409 when the role already exists; treat that as success.
    try:
        _neon_request(
            "POST",
            f"/projects/{project_id}/branches/{branch.id}/roles",
            api_token=api_token,
            json_body={"role": {"name": role_name}},
        )
    except NeonProviderError as exc:
        if "409" not in str(exc):
            raise

    _neon_request(
        "POST",
        f"/projects/{project_id}/branches/{branch.id}/databases",
        api_token=api_token,
        json_body={"database": {"name": database_name, "owner_name": role_name}},
    )

    uri_payload = _neon_request(
        "GET",
        f"/projects/{project_id}/connection_uri?database_name={database_name}&role_name={role_name}&pooled=true",
        api_token=api_token,
    )
    uri = uri_payload.get("uri") if isinstance(uri_payload, dict) else None
    if not isinstance(uri, str) or not uri:
        raise NeonProviderError(f"Neon API did not return a connection URI for database {database_name!r}")

    return NeonDatabaseRecord(
        project_id=project_id,
        branch_id=branch.id,
        database_name=database_name,
        role_name=role_name,
        pooled_dsn=SecretStr(uri),
    )


def delete_neon_database(
    name: DevEnvName,
    *,
    project_id: str,
    api_token: SecretStr,
) -> None:
    """Delete the database created by :func:`create_neon_database` for ``name``.

    Idempotent: returns silently when the database does not exist (HTTP 404).
    """
    branch = _resolve_default_branch(project_id, api_token=api_token)
    database_name = f"minds-dev-{name}"
    try:
        _neon_request(
            "DELETE",
            f"/projects/{project_id}/branches/{branch.id}/databases/{database_name}",
            api_token=api_token,
        )
    except NeonProviderError as exc:
        if "404" in str(exc):
            return
        raise
