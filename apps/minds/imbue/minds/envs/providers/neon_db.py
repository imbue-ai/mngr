"""Create / delete Neon databases via the Neon REST API.

Each dynamic dev env gets its own Neon database, named ``minds-dev-<name>``,
under a shared dev-tier Neon project. Authentication uses an API token the
dev tier owns in Vault (``secrets/minds/dev/neon`` plus the
operator-side Neon project id from ``deploy.toml``).

Returns the pooled connection string the connector / connector clients use
at runtime.
"""

import shutil
from typing import Final

import httpx
from pydantic import Field
from pydantic import SecretStr
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError
from imbue.mngr.utils.polling import poll_for_value

_NEON_API_BASE: Final[str] = "https://console.neon.tech/api/v2"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0

# Neon API operations are async: a POST/DELETE schedules an operation
# and returns immediately, and any subsequent call against the same
# project that targets a still-running operation gets HTTP 423 (Locked).
# Poll-via-operations-API is the documented pattern, but retry-on-423
# at a steady interval is much simpler and equally effective for our
# few-call provisioning flow. The total budget is generous so a slow
# Neon region doesn't trip us up.
_LOCKED_RETRY_POLL_INTERVAL_SECONDS: Final[float] = 2.0
_LOCKED_RETRY_TOTAL_BUDGET_SECONDS: Final[float] = 60.0

# psql shellout for schema-level wipe (Neon REST API doesn't expose
# schema ops). Generous enough to absorb a slow Neon cold-start; short
# enough that a real connectivity failure surfaces in well under a
# minute.
_PSQL_WIPE_TIMEOUT_SECONDS: Final[float] = 60.0


class NeonProviderError(MindError):
    """Raised when the Neon API rejects a request."""


class NeonBranchSummary(FrozenModel):
    """One row of ``GET /projects/{id}/branches``.

    Neon's API returns lots of metadata per branch (project_id, slug,
    project_slug, parent_id, default flags, timestamps, ...). We only
    care about ``id`` and ``name``, so we tell pydantic to drop the rest.
    """

    model_config = {"extra": "ignore", "frozen": True}

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


class _NeonRequestAttempt(FrozenModel):
    """Single-shot callable that runs one Neon HTTP request.

    Returns ``None`` on HTTP 423 to signal ``poll_for_value`` it should
    retry; otherwise returns the raw :class:`httpx.Response` so the
    caller can decode the body / raise on non-423 4xx/5xx. Wrapped as a
    FrozenModel so it's a module-level callable (not a nested ``def``).
    """

    model_config = {"arbitrary_types_allowed": True, "frozen": True}

    method: str
    path: str
    api_token: SecretStr
    json_body: dict | None = None

    def __call__(self) -> httpx.Response | None:
        headers = {
            "Authorization": f"Bearer {self.api_token.get_secret_value()}",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                resp = client.request(
                    self.method,
                    f"{_NEON_API_BASE}{self.path}",
                    headers=headers,
                    json=self.json_body,
                )
        except httpx.HTTPError as exc:
            raise NeonProviderError(f"Neon API request failed ({self.method} {self.path}): {exc}") from exc
        if resp.status_code == 423:
            return None
        return resp


def _neon_request(
    method: str,
    path: str,
    *,
    api_token: SecretStr,
    json_body: dict | None = None,
) -> dict:
    """Issue one Neon API request, retrying on HTTP 423 with a fixed-interval poll.

    Other 4xx/5xx responses fail immediately. 423 (project locked behind
    an in-flight async op) is the only retryable code per Neon's docs --
    we wait for the in-flight op to drain by repeatedly retrying the
    same call until Neon stops locking.
    """
    response, _, _ = poll_for_value(
        _NeonRequestAttempt(method=method, path=path, api_token=api_token, json_body=json_body),
        timeout=_LOCKED_RETRY_TOTAL_BUDGET_SECONDS,
        poll_interval=_LOCKED_RETRY_POLL_INTERVAL_SECONDS,
    )
    if response is None:
        raise NeonProviderError(
            f"Neon API kept returning 423 (Locked) for {method} {path} after "
            f"{_LOCKED_RETRY_TOTAL_BUDGET_SECONDS:.0f}s of retries; an in-flight "
            "operation likely never finished."
        )
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
    try:
        branches = _BRANCH_LIST_ADAPTER.validate_python(branches_raw)
    except ValidationError as exc:
        raise NeonProviderError(f"Neon /projects/{project_id}/branches returned an unexpected shape: {exc}") from exc
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
    """Create (or look up) the per-dev-env database on the dev-tier project.

    Ensures ``role_name`` exists on the project (creating it if not) so
    the database has a non-superuser owner. Idempotent: if the database
    already exists (Neon returns HTTP 409 on duplicate-name create), we
    skip the create and proceed straight to reading the connection URI.
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

    # Same idempotency story for the database itself.
    try:
        _neon_request(
            "POST",
            f"/projects/{project_id}/branches/{branch.id}/databases",
            api_token=api_token,
            json_body={"database": {"name": database_name, "owner_name": role_name}},
        )
    except NeonProviderError as exc:
        if "409" not in str(exc):
            raise

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


def wipe_neon_db_schema(dsn: SecretStr, *, parent_cg: ConcurrencyGroup) -> None:
    """Drop and recreate the ``public`` schema in the database ``dsn`` points at.

    Used by ``minds env destroy --yes-i-mean-staging`` to clear the
    staging Neon DB's tables without deleting the database itself --
    the operator's Vault entry holds the DSN, and we want it to stay
    valid across destroy / redeploy cycles.

    Implementation: shells out to ``psql <dsn>`` with ``DROP SCHEMA public
    CASCADE; CREATE SCHEMA public;``. The Neon REST API does not expose
    schema-level operations, and pulling psycopg into the minds runtime
    just for this single op is more weight than the shellout. ``psql``
    is a standard postgres-client binary; the deploy machine already
    has it (Modal workers / dev laptops both ship it via ``apt install
    postgresql-client`` or homebrew). Raises :class:`NeonProviderError`
    when ``psql`` is missing or the SQL fails -- destroy aborts so the
    operator can fix the underlying issue rather than silently leaving
    the schema half-wiped.

    Idempotent: ``DROP SCHEMA public CASCADE`` succeeds whether or not
    the schema has tables, and ``CREATE SCHEMA public`` recreates an
    empty one.
    """
    psql_path = shutil.which("psql")
    if psql_path is None:
        raise NeonProviderError(
            "psql binary not on PATH; cannot wipe the Neon schema. Install via "
            "`apt install postgresql-client` (Debian/Ubuntu) or `brew install libpq` (macOS)."
        )
    command = [
        psql_path,
        dsn.get_secret_value(),
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
    ]
    cg = parent_cg.make_concurrency_group(name="psql-wipe-neon-schema")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_PSQL_WIPE_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise NeonProviderError(f"`psql` exited {result.returncode} while wiping the Neon schema: {stderr}")
