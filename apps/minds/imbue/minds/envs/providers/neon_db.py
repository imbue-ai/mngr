"""Create / delete per-dev-env Neon *projects* via the Neon REST API.

Each dynamic dev env gets its own Neon project named ``minds-<env>``
under the dev tier's Neon organization. The project contains two
databases on the default branch:

* ``host_pool``    -- the imbue-cloud pool host registry. The
  ``pool_hosts`` schema is applied automatically as part of project
  creation by replaying ``apps/remote_service_connector/migrations/*.sql``
  via psql.
* ``litellm_cost`` -- the LiteLLM proxy's Prisma-managed backing store.
  Empty on creation; the LiteLLM Prisma migration runs against it later
  inside ``deploy_litellm_proxy``.

This shape matches every other dev-tier resource axis (one Modal env,
one SuperTokens app, one Cloudflare-tunnel tag scope, one OVH IAM tag
scope per dev env). Destroy is atomic: ``DELETE /projects/<id>``
removes everything (branches, roles, both DBs, the project's pooler
endpoint) in one call.

Staging / production keep the tier-shared single-DB model: their
``DATABASE_URL`` vault entries are the authoritative DSN, and
``wipe_neon_db_schema`` (still exported here) clears state by
``DROP SCHEMA public`` instead of destroying the project. That model
fits a long-lived shared tier; the project-per-env model fits the
churn of per-developer environments.
"""

import shutil
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import SecretStr
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import info_span
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
_LOCKED_RETRY_TOTAL_BUDGET_SECONDS: Final[float] = 120.0

# psql shellout for schema-level wipe (Neon REST API doesn't expose
# schema ops). Generous enough to absorb a slow Neon cold-start; short
# enough that a real connectivity failure surfaces in well under a
# minute.
_PSQL_TIMEOUT_SECONDS: Final[float] = 60.0

# Default region for new per-env Neon projects. Matches where the
# dev-tier org already lives; tier-shared projects (staging /
# production) are operator-managed and not affected by this.
_DEFAULT_REGION_ID: Final[str] = "aws-us-west-2"
_DEFAULT_PG_VERSION: Final[int] = 17

# Names of the databases we provision inside every per-env project.
# Both names use snake_case so they don't need quoting in psql.
HOST_POOL_DB_NAME: Final[str] = "host_pool"
LITELLM_COST_DB_NAME: Final[str] = "litellm_cost"


class NeonProviderError(MindError):
    """Raised when the Neon API rejects a request."""


class NeonBranchSummary(FrozenModel):
    """One row of ``GET /projects/{id}/branches``."""

    model_config = {"extra": "ignore", "frozen": True}

    id: str
    name: str


class NeonProjectRecord(FrozenModel):
    """Result of :func:`create_neon_project`."""

    project_id: str = Field(description="Neon project id (e.g. `raspy-lake-82340275`).")
    project_name: str = Field(description="Neon project name -- equals `minds-<env-name>`.")
    branch_id: str = Field(description="Neon default branch id (typically the main branch).")
    host_pool_dsn: SecretStr = Field(
        description="Pooled DSN for the `host_pool` database. Used by the connector and `mngr imbue_cloud admin pool create`.",
    )
    litellm_cost_dsn: SecretStr = Field(
        description="Pooled DSN for the `litellm_cost` database. Used by the LiteLLM proxy.",
    )


class NeonProjectSummary(FrozenModel):
    """One row of ``GET /projects?org_id=...``."""

    model_config = {"extra": "ignore", "frozen": True}

    id: str
    name: str
    # Neon returns an ISO 8601 timestamp. Surfacing it here so the
    # multi-match error message in :func:`_select_one_or_raise_multi_match`
    # can sort + display the candidates chronologically -- the operator
    # almost always wants to keep the newest and delete the rest, and
    # creation time is the only available signal for "which one is live"
    # without a separate probe.
    created_at: str = ""


_BRANCH_LIST_ADAPTER: TypeAdapter[list[NeonBranchSummary]] = TypeAdapter(list[NeonBranchSummary])
_PROJECT_LIST_ADAPTER: TypeAdapter[list[NeonProjectSummary]] = TypeAdapter(list[NeonProjectSummary])


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


def _project_name_for(name: DevEnvName) -> str:
    """The Neon project name we use for a dev env.

    Mirrors the rest of the dev-tier naming (Modal env, SuperTokens app
    id, OVH IAM tag scope all just use the env name verbatim). The
    ``minds-`` prefix prevents collisions with unrelated projects in
    the same Neon org.
    """
    return f"minds-{name}"


def _find_projects_by_name(org_id: str, project_name: str, *, api_token: SecretStr) -> list[NeonProjectSummary]:
    """Look up every Neon project named ``project_name`` under ``org_id``.

    We don't persist the project id locally -- destroy on a different
    machine still needs to find the project. Filtering by name + org
    is the canonical lookup pattern for an org-scoped Neon token.

    Returns the empty list when no project matches. Returns a list with
    more than one element when Neon happens to hold duplicates -- Neon
    does not enforce unique project names within an organization, and a
    bug in an earlier ``create_neon_project`` (pre-2026-05) could leave
    several projects with the same name after repeated deploys. Callers
    that need a single project should pipe through
    :func:`_select_one_or_raise_multi_match` so the duplicate case is
    surfaced loudly rather than guessed at.
    """
    payload = _neon_request("GET", f"/projects?org_id={org_id}&limit=400", api_token=api_token)
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list):
        raise NeonProviderError(f"Neon /projects returned no `projects` array; got: {payload!r}")
    try:
        projects = _PROJECT_LIST_ADAPTER.validate_python(raw_projects)
    except ValidationError as exc:
        raise NeonProviderError(f"Neon /projects returned an unexpected shape: {exc}") from exc
    return [project for project in projects if project.name == project_name]


def _select_one_or_raise_multi_match(
    projects: list[NeonProjectSummary],
    project_name: str,
    *,
    org_id: str,
) -> NeonProjectSummary | None:
    """Validate that at most one Neon project matches the requested name.

    Returns ``None`` when nothing matches, the single match when there is
    exactly one, and raises :class:`NeonProviderError` with a copy-
    pasteable cleanup recipe when there are several. We refuse to guess
    in the multi-match case because:

    * Neon does not enforce unique project names within an organization,
      so a name collision is real ambiguity rather than just transient
      duplication.
    * Picking by "first match" (the historical behaviour) silently
      destroys the wrong project on ``minds env destroy`` and silently
      adopts the wrong project on the next ``minds env deploy``. Both
      failure modes have already happened in practice (see F32 in
      ``MANUAL_DEPLOY_FINDINGS.md``).

    Pure function -- no I/O. Lives next to the only callers so the
    error message can be unit-tested without standing up a Neon stub.
    """
    if not projects:
        return None
    if len(projects) == 1:
        return projects[0]
    raise NeonProviderError(_format_multi_match_message(projects, project_name=project_name, org_id=org_id))


def _format_multi_match_message(
    projects: list[NeonProjectSummary],
    *,
    project_name: str,
    org_id: str,
) -> str:
    """Render the operator-facing error for the multi-match case.

    Lists every matching project (oldest-first so the most-recent is at
    the bottom, which is conventionally the one the operator wants to
    keep), and emits a curl recipe per project plus a one-liner that
    deletes every project named ``project_name``. The operator picks
    which to run.
    """
    by_age = sorted(projects, key=lambda p: p.created_at)
    lines = [
        f"Found {len(projects)} Neon projects named {project_name!r} in org {org_id!r}; refusing to guess which one is "
        "live. Neon allows duplicate project names within an org, so this state means an earlier deploy created a "
        "fresh project instead of adopting the existing one. Decide which project to keep (most-recently-created is "
        "usually the live one referenced by the local ``~/.minds-<env>/secrets.toml``), then delete the others via "
        "the Neon API:",
        "",
        "Matching projects (oldest first):",
    ]
    for project in by_age:
        lines.append(f"  - id={project.id}  created_at={project.created_at}")
    lines.append("")
    lines.append("Delete one specific project:")
    lines.append(
        "  NEON_TOKEN=$(vault kv get -format=json secrets/minds/<tier>/neon-admin/NEON_API_TOKEN | jq -r .data.data.value)"
    )
    lines.append(
        '  curl -X DELETE -H "Authorization: Bearer $NEON_TOKEN" "https://console.neon.tech/api/v2/projects/<project-id>"'
    )
    lines.append("")
    lines.append(f"Nuke every project named {project_name!r} (use only if you want to start clean):")
    lines.append(
        "  NEON_TOKEN=$(vault kv get -format=json secrets/minds/<tier>/neon-admin/NEON_API_TOKEN | jq -r .data.data.value)"
    )
    lines.append(f"  for PID in {' '.join(p.id for p in by_age)}; do")
    lines.append(
        '    curl -X DELETE -H "Authorization: Bearer $NEON_TOKEN" "https://console.neon.tech/api/v2/projects/$PID"'
    )
    lines.append("  done")
    lines.append("")
    lines.append("After cleanup, re-run the original ``minds env deploy`` / ``minds env destroy``.")
    return "\n".join(lines)


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


def _ensure_database(
    project_id: str,
    branch_id: str,
    database_name: str,
    *,
    api_token: SecretStr,
) -> None:
    """Create ``database_name`` on the given branch if it does not exist.

    Owner is ``neondb_owner`` (the default role Neon ships with every
    project) -- we don't create extra roles, so the user we authenticate
    as for `host_pool` / `litellm_cost` is the same role that owns the
    default `neondb` database.
    """
    try:
        _neon_request(
            "POST",
            f"/projects/{project_id}/branches/{branch_id}/databases",
            api_token=api_token,
            json_body={"database": {"name": database_name, "owner_name": "neondb_owner"}},
        )
    except NeonProviderError as exc:
        if "409" not in str(exc):
            raise


def _fetch_pooled_dsn(
    project_id: str,
    database_name: str,
    *,
    api_token: SecretStr,
) -> SecretStr:
    payload = _neon_request(
        "GET",
        f"/projects/{project_id}/connection_uri?database_name={database_name}&role_name=neondb_owner&pooled=true",
        api_token=api_token,
    )
    uri = payload.get("uri") if isinstance(payload, dict) else None
    if not isinstance(uri, str) or not uri:
        raise NeonProviderError(f"Neon API did not return a connection URI for database {database_name!r}")
    return SecretStr(uri)


def pool_hosts_migrations_dir() -> Path:
    """Return the directory holding the pool_hosts SQL migrations.

    Hops from this module's location (``apps/minds/imbue/minds/envs/providers/neon_db.py``)
    up to the monorepo root, then down to
    ``apps/remote_service_connector/migrations``. Six ``parents`` hops
    gets us to the repo root.
    """
    repo_root = Path(__file__).resolve().parents[6]
    migrations = repo_root / "apps" / "remote_service_connector" / "migrations"
    if not migrations.is_dir():
        raise NeonProviderError(
            f"Could not locate pool_hosts migrations dir; expected {migrations}. "
            "`minds env deploy` must be run from a checkout of the monorepo."
        )
    return migrations


def create_neon_project(
    name: DevEnvName,
    *,
    org_id: str,
    api_token: SecretStr,
    parent_cg: ConcurrencyGroup,
) -> NeonProjectRecord:
    """Provision (or look up) the per-dev-env Neon project named ``minds-<name>``.

    Steps:

    1. ``POST /projects`` to create the project under ``org_id`` (or
       skip + look up the existing project on collision).
    2. Resolve the default branch.
    3. ``POST .../databases`` for both ``host_pool`` and ``litellm_cost``.
       Both owned by ``neondb_owner`` (the default role).
    4. ``GET .../connection_uri?pooled=true`` for each DB.
    5. Apply the pool_hosts schema migrations to ``host_pool`` via psql.

    Idempotent: every step tolerates pre-existing resources, so calling
    this again after a partial deploy (or as part of a re-deploy)
    converges on the same state.

    Transactional cleanup: if any step after the ``POST /projects``
    fails (DB creation, DSN fetch, or schema apply), the just-created
    project is deleted before the exception propagates. Without this,
    a failure in the late steps would leak the Neon project (the
    outer ``deploy_dev_env`` rollback only sees "neon_project" as a
    completed step after this function returns, so a mid-function
    failure would otherwise orphan the project entirely).
    """
    project_name = _project_name_for(name)

    # Lookup-first. Neon does NOT 409 on duplicate project names within
    # an org -- POSTing the same name twice creates two distinct
    # projects with different ids. So we look up by name before posting,
    # adopt the unique existing project when there is one, and refuse
    # loudly when several already exist (see
    # :func:`_select_one_or_raise_multi_match` for the rationale +
    # operator recipe).
    with info_span("Looking up existing Neon project {!r} under org {}", project_name, org_id):
        candidates = _find_projects_by_name(org_id, project_name, api_token=api_token)
        existing = _select_one_or_raise_multi_match(candidates, project_name, org_id=org_id)

    project_was_pre_existing = existing is not None
    if existing is not None:
        project_id = existing.id
        logger.info("Adopted pre-existing Neon project {!r} (id={})", project_name, project_id)
    else:
        with info_span("Creating Neon project {!r} under org {}", project_name, org_id):
            create_payload = _neon_request(
                "POST",
                "/projects",
                api_token=api_token,
                json_body={
                    "project": {
                        "name": project_name,
                        "org_id": org_id,
                        "pg_version": _DEFAULT_PG_VERSION,
                        "region_id": _DEFAULT_REGION_ID,
                    },
                },
            )
            project_id = create_payload.get("project", {}).get("id")
            if not isinstance(project_id, str) or not project_id:
                raise NeonProviderError(
                    f"Neon POST /projects returned no project.id for {project_name!r}; got: {create_payload!r}"
                )

    try:
        branch = _resolve_default_branch(project_id, api_token=api_token)
        with info_span("Creating Neon database {!r} on branch {}", HOST_POOL_DB_NAME, branch.id):
            _ensure_database(project_id, branch.id, HOST_POOL_DB_NAME, api_token=api_token)
        with info_span("Creating Neon database {!r} on branch {}", LITELLM_COST_DB_NAME, branch.id):
            _ensure_database(project_id, branch.id, LITELLM_COST_DB_NAME, api_token=api_token)

        with info_span("Fetching pooled DSNs for both databases"):
            host_pool_dsn = _fetch_pooled_dsn(project_id, HOST_POOL_DB_NAME, api_token=api_token)
            litellm_cost_dsn = _fetch_pooled_dsn(project_id, LITELLM_COST_DB_NAME, api_token=api_token)
    except NeonProviderError:
        # Best-effort: delete the just-created project before re-raising
        # so a retry starts from a clean slate. If we adopted an
        # operator-managed pre-existing project, we leave it alone --
        # the operator did not ask us to manage its lifecycle.
        if not project_was_pre_existing:
            try:
                _neon_request("DELETE", f"/projects/{project_id}", api_token=api_token)
            except NeonProviderError:
                # Swallow cleanup errors; the original failure is what
                # the caller needs to see. A leaked project will be
                # picked up by the next deploy via the by-name lookup.
                pass
        raise

    return NeonProjectRecord(
        project_id=project_id,
        project_name=project_name,
        branch_id=branch.id,
        host_pool_dsn=host_pool_dsn,
        litellm_cost_dsn=litellm_cost_dsn,
    )


def delete_neon_project(
    name: DevEnvName,
    *,
    org_id: str,
    api_token: SecretStr,
) -> None:
    """Delete the per-dev-env Neon project named ``minds-<name>``.

    Looks up the project by name under ``org_id`` so destroy works from
    any machine (no local project-id pointer required). Idempotent: a
    missing project is treated as success.
    """
    project_name = _project_name_for(name)
    candidates = _find_projects_by_name(org_id, project_name, api_token=api_token)
    # Multi-match: refuse loudly via the same path as create. If the
    # operator destroys here we'd otherwise pick "the first match" --
    # which is whichever happens to be returned first by Neon (typically
    # the OLDEST). That's almost certainly an orphan, not the live
    # project the operator meant to destroy -- leaves the live one
    # stranded.
    existing = _select_one_or_raise_multi_match(candidates, project_name, org_id=org_id)
    if existing is None:
        return
    try:
        _neon_request("DELETE", f"/projects/{existing.id}", api_token=api_token)
    except NeonProviderError as exc:
        if "404" in str(exc):
            return
        raise


def create_snapshot_branch(project_id: str, parent_branch_id: str, name: str, *, api_token: SecretStr) -> str:
    """Snapshot ``parent_branch_id`` by creating a child branch at the current LSN.

    Returns the new branch id. Neon's branch creation is lazy + COW
    (copy-on-write), so the snapshot itself costs almost nothing
    until/unless writes diverge between the snapshot and the parent.

    Used by ``minds env deploy`` as the pre-deploy snapshot: if anything
    fails, :func:`restore_branch_from_snapshot` rewinds the parent
    branch back to this point.

    NOTE: Unlike a hypothetical "named restore-point" API, this leaves
    a real branch in the project. Successful deploys should delete it
    via :func:`delete_neon_branch` to keep the project tidy; failed
    deploys leave it for the restore path.
    """
    payload = _neon_request(
        "POST",
        f"/projects/{project_id}/branches",
        api_token=api_token,
        json_body={"branch": {"name": name, "parent_id": parent_branch_id}},
    )
    branch = payload.get("branch", {}) if isinstance(payload, dict) else {}
    branch_id = branch.get("id")
    if not isinstance(branch_id, str) or not branch_id:
        raise NeonProviderError(f"Neon POST /projects/{project_id}/branches returned no branch.id; got: {payload!r}")
    return branch_id


def restore_branch_from_snapshot(
    project_id: str,
    target_branch_id: str,
    source_branch_id: str,
    preserve_under_name: str,
    *,
    api_token: SecretStr,
) -> None:
    """Restore ``target_branch_id`` to the state of ``source_branch_id``.

    Neon's "instant restore" rewinds ``target_branch_id`` (typically
    the default ``main``) to the state captured by ``source_branch_id``
    (a snapshot branch created earlier via :func:`create_snapshot_branch`).
    Both databases on the target branch (host_pool + litellm_cost) come
    back to that exact state atomically.

    ``preserve_under_name`` is the name Neon assigns to a fresh branch
    holding the PRE-restore state of the target (so the operator can
    inspect the broken state later). Pass something descriptive like
    ``pre-rollback-<deploy_id>``.

    Idempotent: re-running after a restore that already used this
    ``preserve_under_name`` is a no-op. Neon names the pre-restore
    preservation branch ``preserve_under_name``, so a second restore with
    the same name returns 409 ("branch with that name already exists") --
    which we treat as "already restored" rather than surfacing as an error.
    Without this, a recover that failed a *later* step (e.g. the Modal app
    stop) could never be re-run to completion, wedging its recover-target
    file in place forever.
    """
    try:
        _neon_request(
            "POST",
            f"/projects/{project_id}/branches/{target_branch_id}/restore",
            api_token=api_token,
            json_body={
                "source_branch_id": source_branch_id,
                "preserve_under_name": preserve_under_name,
            },
        )
    except NeonProviderError as exc:
        message = str(exc)
        if "409" in message and "already exists" in message:
            logger.info(
                "Skipped Neon restore of branch {!r}: preserve branch {!r} already exists "
                "(restore was already applied by a prior run).",
                target_branch_id,
                preserve_under_name,
            )
            return
        raise


def delete_neon_branch(project_id: str, branch_id: str, *, api_token: SecretStr) -> None:
    """Delete a Neon branch. Used after a successful deploy to clean up the snapshot.

    Idempotent: 404 treated as success. May fail with HTTP 422
    "cannot delete branch that has children" if the branch was used as
    a source for a restore (Neon re-parents the target onto the
    source). That's fine: failed deploys take the restore path which
    doesn't delete the snapshot anyway; the snapshot stays in the
    project's history as the operator's "pre-deploy state" copy.
    """
    try:
        _neon_request("DELETE", f"/projects/{project_id}/branches/{branch_id}", api_token=api_token)
    except NeonProviderError as exc:
        if "404" in str(exc):
            return
        raise


def verify_neon_token_has_restore_scope(project_id: str, *, api_token: SecretStr) -> None:
    """Preflight check: confirm the configured Neon API token can read the project.

    A read of ``GET /projects/{id}`` is the cheapest probe that exercises
    the same authorization path the snapshot + restore calls will use.
    Failure surfaces as :class:`NeonProviderError` so the operator sees
    "Neon API returned 403" before deploy starts mutating anything.
    """
    _neon_request("GET", f"/projects/{project_id}", api_token=api_token)


def resolve_default_branch_id(project_id: str, *, api_token: SecretStr) -> str:
    """Public helper: return the default branch id on a project.

    Shared-tier deploys (``creates_resources=false``) use this to
    resolve the branch id at deploy time -- the operator brings a Neon
    project + DSN via Vault but doesn't need to track the branch id
    separately.
    """
    return _resolve_default_branch(project_id, api_token=api_token).id


def wipe_neon_db_schema(dsn: SecretStr, *, parent_cg: ConcurrencyGroup) -> None:
    """Drop and recreate the ``public`` schema in the database ``dsn`` points at.

    Used by ``minds env destroy --yes-i-mean-staging`` to clear the
    staging Neon DB's tables without deleting the database itself --
    the operator's Vault entry holds the DSN, and we want it to stay
    valid across destroy / redeploy cycles. Dev envs don't go through
    this path (they delete the whole project via
    :func:`delete_neon_project`).

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
            timeout=_PSQL_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise NeonProviderError(f"`psql` exited {result.returncode} while wiping the Neon schema: {stderr}")
