"""Orchestrate ``minds env deploy / list / destroy`` flows.

Split into two deploy paths driven by the activated env's tier:

* :func:`deploy_dev_env` -- for the ``dev`` tier. Per-env Modal env,
  Neon DB, SuperTokens app, then per-env Modal Secret pushes and
  Modal app deploys. On success writes split files under
  ``~/.minds-<env-name>/``: a non-secret ``client.toml`` (mode 0644)
  and a chmod-0600 ``secrets.toml`` carrying the values
  (Neon DSN, SuperTokens connection URI + API key) the operator needs
  to re-deploy in place.
* :func:`deploy_tier_env` -- for ``staging`` / ``production``. Pushes
  tier-shared secrets straight from Vault to Modal (no per-env
  overrides) and deploys both Modal apps into the tier's stable Modal
  env (``main`` by convention; settable via ``deploy.toml``).
  **Writes nothing to disk** -- the URLs are deterministic from the
  tier's Modal workspace + app names, and the committed in-repo
  ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` is the
  source of truth.

The orchestration is pure logic; the CLI plumbing in
``imbue.minds.cli.env`` builds the :class:`Providers` bundle with the
real Modal CLI / Neon HTTP / SuperTokens HTTP / OVH HTTP / Modal
deploy callables, and dispatches to the right deploy function based
on the activated env's name.

``deploy_dev_env`` is idempotent: re-running it for an existing dev env
re-pushes Modal Secrets and re-deploys both Modal apps, picking up any
new tier-shared values that landed in Vault since the last run. The
local files are overwritten in place; there is no "already exists"
gate.

Failure model: if any *provider creation* step (Modal env, Neon DB,
SuperTokens app) fails partway through on a fresh deploy, the helper
rolls back whatever it just created and re-raises. The push-secrets and
Modal-deploy steps are intrinsically idempotent (Modal Secret upserts
with ``--force``, Modal deploys overwrite), so they don't need rollback
-- the operator can just re-run ``minds env deploy``.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import info_span
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.data_types import DeployLifecycleConfig
from imbue.minds.config.data_types import ModalEnvStrategy
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_client_config
from imbue.minds.config.loader import repo_tier_client_config_path
from imbue.minds.envs.generation import GENERATION_ID_KEY
from imbue.minds.envs.local_store import client_config_exists
from imbue.minds.envs.local_store import delete_env_root
from imbue.minds.envs.local_store import env_root_exists
from imbue.minds.envs.local_store import read_client_config_file
from imbue.minds.envs.local_store import write_client_config
from imbue.minds.envs.local_store import write_secrets_file
from imbue.minds.envs.mngr_agent_cleanup import DestroyMngrAgentsFn
from imbue.minds.envs.mngr_agent_cleanup import destroy_all_mngr_agents_in_env
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.paths import list_env_root_dirs
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.per_env_deploy import build_per_env_secret_values
from imbue.minds.envs.per_env_deploy import delete_modal_secret
from imbue.minds.envs.per_env_deploy import deploy_litellm_proxy
from imbue.minds.envs.per_env_deploy import deploy_remote_service_connector
from imbue.minds.envs.per_env_deploy import ensure_modal_env
from imbue.minds.envs.per_env_deploy import per_env_connector_url
from imbue.minds.envs.per_env_deploy import per_env_litellm_proxy_url
from imbue.minds.envs.per_env_deploy import push_per_env_modal_secret
from imbue.minds.envs.per_env_deploy import stop_modal_app
from imbue.minds.envs.per_env_deploy import tier_connector_url
from imbue.minds.envs.per_env_deploy import tier_litellm_proxy_url
from imbue.minds.envs.primitives import DeployStrategy
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.ovh_tags import OvhCredentials
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.supertokens_app import app_id_from_connection_uri
from imbue.minds.envs.recover import RecoverTarget
from imbue.minds.envs.recover import delete_recover_target
from imbue.minds.envs.recover import find_monorepo_root
from imbue.minds.envs.recover import hold_deploy_lock
from imbue.minds.envs.recover import make_neon_snapshot_branch_name
from imbue.minds.envs.recover import recover_target_exists
from imbue.minds.envs.recover import recover_target_path
from imbue.minds.envs.recover import write_recover_target_atomic
from imbue.minds.envs.secret_lifecycle import gc_old_per_tier_secrets
from imbue.minds.envs.secret_lifecycle import make_deploy_id
from imbue.minds.envs.secret_lifecycle import timestamped_secret_name
from imbue.minds.errors import MindError
from imbue.mngr_ovh.iam_tags import IamResource

# Env var the deployed connector reads at startup to identify which
# minds env it belongs to. Pushed alongside ``MINDS_TIER_GENERATION_ID``
# in the per-env ``litellm-connector-<tier>`` Modal Secret. For dev-tier
# deploys this is the per-developer dev env name (e.g. ``dev-josh-3``); for
# tier deploys it's the tier itself (``staging`` / ``production``).
# Used by ``cf_create_tunnel`` to tag every Cloudflare tunnel the
# connector creates with the owning env, so ``minds env destroy`` can
# enumerate + delete only that env's tunnels (vs walking every tunnel
# on the shared dev-tier CF account).
MINDS_ENV_NAME_KEY: Final[str] = "MINDS_ENV_NAME"

# Env-var name the deployed connector reads at request time to drive
# its broken-healthcheck injection (see app.py::get_health_liveness).
# When set in the operator's environment at `minds env deploy` time,
# we propagate it into the per-deploy litellm-connector Modal Secret
# so the deployed container sees it. This is the injection point
# `test_deploy_rollback` uses to drive the auto-rollback path.
INJECT_BROKEN_HEALTHCHECK_ENV_VAR: Final[str] = "MINDS_INJECT_BROKEN_HEALTHCHECK"


class ProviderCredentials(FrozenModel):
    """Per-provider credentials read from the dev-tier Vault secrets.

    Each dynamic dev env shares these dev-tier creds (the user's whole
    point in flagging that dev secrets stay local-only): minds reads them
    fresh from Vault for the duration of an ``env deploy`` invocation
    and does not persist them.
    """

    neon_org_id: str = Field(
        description=(
            "Neon organization id under which per-dev-env Neon *projects* are created "
            "(one project per dev env named ``minds-<env>``). Operator-managed; lives in "
            "``secrets/minds/dev/neon-admin/NEON_ORG_ID``. Required for the dev tier; "
            "may be the empty string for shared tiers (which never call POST /projects)."
        ),
    )
    neon_api_token: SecretStr = Field(
        description=(
            "Neon API token. Dev tier requires project-create + branch-create + restore "
            "scope on the dev org; shared tiers (staging / production) just need branch-"
            "create + restore scope on the operator-managed project (read via "
            "``neon_project_id``)."
        ),
    )
    neon_project_id: str | None = Field(
        default=None,
        description=(
            "Existing Neon project id for shared tiers (``creates_resources=false``). "
            "Read from ``secrets/minds/<tier>/neon-admin/NEON_PROJECT_ID``. Required for "
            "shared-tier deploys so the pre-deploy snapshot + rollback restore can target "
            "the right project; ``None`` for dev (where the project is created per-env)."
        ),
    )
    supertokens_core_url: str = Field(description="Dev-tier SuperTokens core base URL.")
    supertokens_api_key: SecretStr = Field(description="Dev-tier SuperTokens admin API key.")
    ovh_credentials: OvhCredentials = Field(description="Dev-tier OVH AK/AS/CK credentials (shared across dev envs).")


# Tiers whose default deploy strategy is RECREATE: dev (every personal
# dev env) and ci (every CI ephemeral env stood up by the deployment-
# tests orchestrator). Operator deploys against these almost always
# follow a "deploy then immediately observe" pattern where a stale warm
# container that keeps serving the prior version's behavior is the
# opposite of what the operator wants. Shared tiers (staging,
# production) fall through to ROLLOVER for zero-downtime.
_DEFAULT_RECREATE_TIERS: Final[frozenset[str]] = frozenset({"dev", "ci"})


def resolve_deploy_strategy(
    *,
    explicit: DeployStrategy | None,
    tier: str,
    had_migrations: bool,
) -> DeployStrategy:
    """Pick the Modal deploy strategy from operator override + deploy context.

    Policy (operator-documented):

    1. ``explicit`` wins. ``--hard`` / ``--soft`` on the CLI resolve to
       :attr:`DeployStrategy.RECREATE` / :attr:`DeployStrategy.ROLLOVER`
       respectively and we always honour them. Pass ``None`` to defer
       to the default policy.
    2. If a migration was applied this deploy, use ``RECREATE``. Old
       code against a freshly-migrated DB schema is a combination the
       deployed binary has not been tested against; cycling all
       containers immediately is the safe choice even for a
       "backwards-compatible" migration.
    3. Otherwise, ``RECREATE`` for the per-env tiers (``dev`` for
       personal dev envs and ``ci`` for CI ephemeral envs stood up by
       the deployment-tests orchestrator). The operator's flow on
       these tiers is "deploy and immediately observe", and the
       stale-warm-container window (several minutes for Modal's
       default rollover) silently masks new code's behavior.
    4. Otherwise (staging / production with no migration applied),
       ``ROLLOVER``. Shared tiers prioritize zero-downtime; the
       operator can opt in to ``RECREATE`` with ``--hard`` when they
       need it.
    """
    if explicit is not None:
        return explicit
    if had_migrations:
        return DeployStrategy.RECREATE
    if tier in _DEFAULT_RECREATE_TIERS:
        return DeployStrategy.RECREATE
    return DeployStrategy.ROLLOVER


# Type aliases for the injectable provider callables. Tests substitute
# fakes here; the CLI wires the real provider modules at runtime.
# ``CreateNeonProjectFn`` provisions a per-dev-env Neon project (named
# ``minds-<env>``) that holds the env's ``host_pool`` + ``litellm_cost``
# databases. Signature: ``(name, org_id, api_token, parent_cg) ->
# NeonProjectRecord``.
CreateNeonProjectFn = Callable[[DevEnvName, str, SecretStr, ConcurrencyGroup], NeonProjectRecord]
DeleteNeonProjectFn = Callable[[DevEnvName, str, SecretStr], None]
CreateSuperTokensAppFn = Callable[[DevEnvName, str, SecretStr], SuperTokensAppRecord]
DeleteSuperTokensAppFn = Callable[[DevEnvName, str, SecretStr], None]
ListOvhInstancesFn = Callable[[DevEnvName, OvhCredentials], tuple[IamResource, ...]]
DeleteOvhInstancesFn = Callable[[tuple[IamResource, ...], OvhCredentials], None]
ModalEnvOpFn = Callable[[DevEnvName, ConcurrencyGroup], None]
PushPerEnvSecretFn = Callable[[str, dict[str, str], str, ConcurrencyGroup], None]
# (modal_env, tier, min_containers, deploy_id, strategy, cg) -> deployed URL.
# ``modal_env`` is the dev env name for dev-tier deploys or the tier's
# stable Modal env (``main`` by convention) for shared-tier deploys.
# ``min_containers`` is the per-app warm-pool size from the tier's
# ``[min_containers]`` deploy.toml block. ``deploy_id`` is the
# UTC-timestamp deploy id minted at the start of this deploy run;
# the implementation threads it into the modal subprocess env as
# ``MINDS_DEPLOY_ID`` so the deployed app pins to the matching
# ``<svc>-<tier>-<id>`` Modal Secrets. ``strategy`` is forwarded to
# ``modal deploy --strategy`` so a single deploy can cycle both apps
# the same way (see :class:`DeployStrategy`).
# (modal_env, tier, min_containers, scaledown_window, deploy_id, strategy, cg) -> deployed URL.
DeployModalAppFn = Callable[[str, str, int, int, str, DeployStrategy, ConcurrencyGroup], AnyUrl]
# (app_name, modal_env, cg) -> None. Used by tier destroys to ``modal
# app stop`` each deployed app. Idempotent in the underlying call.
StopModalAppFn = Callable[[str, str, ConcurrencyGroup], None]
# (secret_name, modal_env, cg) -> None. Used by tier destroys to
# ``modal secret delete`` each pushed per-tier Modal Secret. Idempotent.
DeleteModalSecretFn = Callable[[str, str, ConcurrencyGroup], None]
# (modal_env, cg) -> tuple of secret names in the Modal env. Used by
# the timestamped-secret GC to find old ``<svc>-<tier>-<id>`` entries
# to delete after a successful deploy.
ListModalSecretsFn = Callable[[str, ConcurrencyGroup], tuple[str, ...]]
# (host_pool_dsn, cg) -> tuple of applied migration Paths. Runs the
# schema_migrations runner against the per-env host_pool DB. Tests
# pass a no-op fake; the real implementation shells out to psql.
ApplyPoolHostsMigrationsFn = Callable[[SecretStr, ConcurrencyGroup], tuple[Path, ...]]
# (host_pool_dsn, domains, emails, cg) -> None. Seed-if-absent default paid
# domains/emails into the host_pool DB after migrations. Tests pass a no-op fake.
SeedPaidListDefaultsFn = Callable[[SecretStr, tuple[str, ...], tuple[str, ...], ConcurrencyGroup], None]
# (app_name, modal_env, cg) -> latest deployed version id, or None for
# never-deployed. Used at deploy start to capture pre-deploy state so
# ``minds env recover`` can `modal app rollback` to it on failure.
GetModalAppLatestVersionFn = Callable[[str, str, ConcurrencyGroup], str | None]
# (app_name, version, modal_env, cg) -> None. Used by `minds env recover`
# to roll a Modal app back to its pre-deploy version.
RollbackModalAppFn = Callable[[str, str, str, ConcurrencyGroup], None]
# (project_id, parent_branch_id, snapshot_name, api_token) -> new branch id.
# Creates a child branch off the parent at the current LSN so recover
# can `restore` the parent from this snapshot.
CreateNeonSnapshotBranchFn = Callable[[str, str, str, SecretStr], str]
# (project_id, branch_id, api_token) -> None. Deletes a Neon branch.
# Used at end of successful deploy to clean up the just-created snapshot.
DeleteNeonBranchFn = Callable[[str, str, SecretStr], None]
# (project_id, api_token) -> default-branch id. Used by shared-tier
# deploys to look up the operator-managed project's main branch.
ResolveNeonDefaultBranchFn = Callable[[str, SecretStr], str]
# (project_id, api_token) -> None. Preflight probe; raises NeonProviderError
# on insufficient scope.
VerifyNeonScopeFn = Callable[[str, SecretStr], None]
# (connector_url, litellm_proxy_url) -> None. Polls both apps' health
# endpoints until both return 200 (or until the per-app polling budget
# runs out). Raises HealthCheckFailedError on definitive failure or
# timeout. Tests inject a no-op fake; the real one shells out to httpx.
AwaitAppsHealthyFn = Callable[[AnyUrl, AnyUrl], None]
# (app_id, core_base_url, api_key) -> None. Used by tier destroys to
# wipe every user/session in an existing SuperTokens app without
# deleting the app itself. Idempotent via delete + recreate.
WipeSuperTokensAppFn = Callable[[str, str, SecretStr], None]
# (dsn, cg) -> None. Used by tier destroys to drop + recreate the
# ``public`` schema in the Neon DB the DSN points at.
WipeNeonSchemaFn = Callable[[SecretStr, ConcurrencyGroup], None]
# (tier_vault_prefix, cg) -> generation_id. Mints + writes if missing,
# otherwise returns the existing id. Used by tier deploys.
EnsureGenerationIdFn = Callable[[str, ConcurrencyGroup], str]
# (tier_vault_prefix, cg) -> None. Removes the generation Vault entry
# so the next deploy mints a fresh one. Used by tier destroys.
DeleteGenerationIdFn = Callable[[str, ConcurrencyGroup], None]
# (name, account_id, api_token) -> tuple of tunnel uuids matching env.
ListCloudflareTunnelsFn = Callable[[DevEnvName, str, SecretStr], tuple[str, ...]]
# (tunnel_ids, account_id, api_token) -> None. Deletes the listed tunnels.
DeleteCloudflareTunnelsFn = Callable[[tuple[str, ...], str, SecretStr], None]
ReadPerEnvSecretValuesFn = Callable[[str, str, dict[str, str], ConcurrencyGroup], dict[str, str]]
# (name, cg) -> None. Removes the env's mngr Docker state container +
# backing volume, targeting the one exact container by name. No-op when
# there is no Docker daemon. Real impl lives in ``envs.docker_cleanup``.
CleanupStateContainerFn = Callable[[DevEnvName, ConcurrencyGroup], None]


class Providers(FrozenModel):
    """Injectable provider bundle.

    All fields are required so tests can't silently get default no-op
    behaviour by forgetting one.
    """

    model_config = {"arbitrary_types_allowed": True}

    ensure_modal_env: ModalEnvOpFn = Field(description="Create the Modal environment (idempotent).")
    delete_modal_env: ModalEnvOpFn = Field(description="Delete the Modal environment.")
    create_neon_project: CreateNeonProjectFn = Field(
        description=(
            "Create or look up the per-dev-env Neon project (one per env, named "
            "``minds-<env>``). Bootstraps the ``host_pool`` + ``litellm_cost`` "
            "databases inside and returns DSNs for both."
        ),
    )
    delete_neon_project: DeleteNeonProjectFn = Field(
        description="Delete the per-dev-env Neon project (atomic teardown of all its DBs / roles / endpoints).",
    )
    create_supertokens_app: CreateSuperTokensAppFn = Field(description="Create the per-dev-env SuperTokens app.")
    delete_supertokens_app: DeleteSuperTokensAppFn = Field(description="Delete the per-dev-env SuperTokens app.")
    list_ovh_instances: ListOvhInstancesFn = Field(description="List OVH VPSes tagged for this dev env.")
    delete_ovh_instances: DeleteOvhInstancesFn = Field(description="Delete the listed OVH VPSes.")
    read_per_env_secret_values: ReadPerEnvSecretValuesFn = Field(
        description="(service, tier_vault_prefix, overrides, cg) -> merged values dict for one Modal Secret.",
    )
    push_per_env_modal_secret: PushPerEnvSecretFn = Field(
        description="(secret_name, values, modal_env, cg) -> upsert the Modal Secret in the named Modal env.",
    )
    deploy_litellm_proxy: DeployModalAppFn = Field(
        description="(modal_env, tier, cg) -> `modal deploy` the llm app into ``modal_env``.",
    )
    deploy_remote_service_connector: DeployModalAppFn = Field(
        description="(modal_env, tier, cg) -> `modal deploy` the connector app into ``modal_env``.",
    )
    stop_modal_app: StopModalAppFn = Field(
        description="(app_name, modal_env, cg) -> `modal app stop` the named app. Idempotent.",
    )
    delete_modal_secret: DeleteModalSecretFn = Field(
        description="(secret_name, modal_env, cg) -> `modal secret delete` the named secret. Idempotent.",
    )
    list_modal_secrets: ListModalSecretsFn = Field(
        description="(modal_env, cg) -> tuple of all Modal Secret names in the env. Used by the timestamped-secret GC.",
    )
    apply_pool_hosts_migrations: ApplyPoolHostsMigrationsFn = Field(
        description=(
            "(host_pool_dsn, cg) -> tuple of applied migration files. "
            "Runs the schema_migrations runner against the per-env host_pool DB."
        ),
    )
    seed_paid_list_defaults: SeedPaidListDefaultsFn = Field(
        description=(
            "(host_pool_dsn, domains, emails, cg) -> seed-if-absent the tier's default "
            "paid domains/emails into the host_pool DB after migrations."
        ),
    )
    get_modal_app_latest_version: GetModalAppLatestVersionFn = Field(
        description="(app_name, modal_env, cg) -> latest deployed version id, or None for never-deployed.",
    )
    rollback_modal_app: RollbackModalAppFn = Field(
        description="(app_name, version, modal_env, cg) -> `modal app rollback` to the given version.",
    )
    create_neon_snapshot_branch: CreateNeonSnapshotBranchFn = Field(
        description=(
            "(project_id, parent_branch_id, name, api_token) -> new branch id. "
            "Snapshots the parent branch by creating a child branch at the current LSN."
        ),
    )
    delete_neon_branch: DeleteNeonBranchFn = Field(
        description="(project_id, branch_id, api_token) -> None. Deletes a Neon branch (snapshot cleanup).",
    )
    resolve_neon_default_branch_id: ResolveNeonDefaultBranchFn = Field(
        description="(project_id, api_token) -> default-branch id. Used by shared-tier deploys.",
    )
    verify_neon_token_has_restore_scope: VerifyNeonScopeFn = Field(
        description="(project_id, api_token) -> probe call that raises NeonProviderError on insufficient scope.",
    )
    await_apps_healthy: AwaitAppsHealthyFn = Field(
        description=(
            "(connector_url, litellm_proxy_url) -> polls both apps' health endpoints until "
            "both return 200 (per-app polling budget). Raises HealthCheckFailedError on "
            "definitive failure or timeout."
        ),
    )
    destroy_mngr_agents: DestroyMngrAgentsFn = Field(
        description=(
            "(agent_ids, mngr_host_dir, mngr_prefix, cg) -> single `mngr destroy -f <ids...>` "
            "with the env's MNGR_* vars exported. Used before cloud teardown so the env's "
            "agents stop cleanly before their resources go away."
        ),
    )
    cleanup_state_container: CleanupStateContainerFn = Field(
        description=(
            "(name, cg) -> remove the env's mngr Docker state container + backing volume. "
            "Targets the one exact container by name; no-op when there is no Docker daemon. "
            "Run right after the mngr-agent teardown so the singleton state container "
            "(which `mngr destroy` does not touch) doesn't outlive the env."
        ),
    )
    wipe_supertokens_app_data: WipeSuperTokensAppFn = Field(
        description=(
            "(app_id, core_base_url, api_key) -> wipe all users / sessions in the named "
            "SuperTokens app without deleting the app itself. Used for tier destroys where "
            "the app is operator-managed and must keep its connection URI / API key."
        ),
    )
    wipe_neon_db_schema: WipeNeonSchemaFn = Field(
        description=(
            "(dsn,) -> DROP SCHEMA public CASCADE; CREATE SCHEMA public; against the DSN. "
            "Used for tier destroys where the Neon DB is operator-managed and must keep its DSN."
        ),
    )
    ensure_generation_id: EnsureGenerationIdFn = Field(
        description=(
            "(tier_vault_prefix, cg) -> generation id. Mints + writes a fresh uuid to "
            "secrets/minds/<tier>/generation if no entry exists; otherwise returns the existing id."
        ),
    )
    delete_generation_id: DeleteGenerationIdFn = Field(
        description=(
            "(tier_vault_prefix, cg) -> None. Removes secrets/minds/<tier>/generation so the "
            "next deploy mints a fresh id (triggers activate-time auto-wipe on every dev's machine)."
        ),
    )
    list_cloudflare_tunnels_for_env: ListCloudflareTunnelsFn = Field(
        description=(
            "(name, account_id, api_token) -> tuple of cloudflare tunnel uuids whose metadata.env "
            "equals the env name. Used by destroy to enumerate the env's tunnels."
        ),
    )
    delete_cloudflare_tunnels: DeleteCloudflareTunnelsFn = Field(
        description=(
            "(tunnel_ids, account_id, api_token) -> None. Deletes the listed cloudflare tunnels. "
            "Idempotent per-tunnel (404 -> success)."
        ),
    )


class DeployedEnv(FrozenModel):
    """Summary returned by :func:`deploy_env` for every tier.

    Carries the URLs Modal assigned the two deployed apps for logging,
    plus the paths of any local-state files written. For tiers whose
    ``[lifecycle].writes_local_state`` is ``false``, ``client_config_path``
    and ``secrets_path`` are ``None``.
    """

    name: DevEnvName = Field(description="The activated env name (dev env name or reserved tier name).")
    tier: str
    modal_env: str = Field(description="The Modal env the apps deployed into.")
    connector_url: AnyUrl
    litellm_proxy_url: AnyUrl
    client_config_path: str | None = Field(
        default=None,
        description="Path to the per-env client.toml, or None when ``[lifecycle].writes_local_state`` is false.",
    )
    secrets_path: str | None = Field(
        default=None,
        description="Path to the per-env secrets.toml, or None when ``[lifecycle].writes_local_state`` is false.",
    )


class DevEnvSummary(FrozenModel):
    """One row of :func:`list_dev_envs`.

    ``client_config_source`` makes the asymmetry between dev envs and
    reserved tiers visible: dev envs have their ``client.toml`` under
    the env root; reserved tiers (``production`` / ``staging``) use
    the committed in-repo ``apps/minds/imbue/minds/config/envs/<tier>/
    client.toml`` even when the env root exists.

    ``connector_url`` is None only when no ``client.toml`` could be
    located (a freshly-mkdir'd dev env that hasn't been deployed yet,
    or a partial deploy that failed before writing the file).
    """

    name: str = Field(description="The env name (e.g. 'dev-josh-3'), or 'production' for ~/.minds/.")
    env_root: str = Field(description="Absolute path to the env root directory on disk.")
    client_config_path: str | None = Field(
        default=None,
        description=(
            "Path to the client.toml driving this env. For dev envs, the per-env "
            "``~/.minds-<env>/client.toml`` (written by ``minds env deploy``). For "
            "reserved tiers, the committed in-repo "
            "``apps/minds/imbue/minds/config/envs/<tier>/client.toml``. None when "
            "neither location holds a parseable client.toml (e.g. unprovisioned dev env)."
        ),
    )
    client_config_source: str | None = Field(
        default=None,
        description=(
            "Where the client.toml lives: 'env_root' for dev envs, 'in_repo' for "
            "reserved tiers (staging / production). None when there's no client.toml."
        ),
    )
    connector_url: AnyUrl | None = Field(
        default=None,
        description="connector_url parsed from the resolved client.toml, or None if no client.toml exists.",
    )


def deploy_env(
    name: DevEnvName,
    *,
    tier: str,
    deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
    explicit_strategy: DeployStrategy | None = None,
) -> DeployedEnv:
    """Provision (or upgrade) the activated env -- unified path for every tier.

    Driven by ``deploy_config.lifecycle``:

    * ``creates_resources=true`` -> create per-env Modal env, Neon project,
      and SuperTokens app outright (today: ``dev`` only).
    * ``creates_resources=false`` -> the Modal env, Neon project, and
      SuperTokens app are operator-managed; deploy reads their values out
      of Vault and never calls a create/delete endpoint for them
      (today: ``staging`` / ``production``).
    * ``modal_env_strategy=PER_ENV`` -> apps deploy into Modal env
      named after the activated dev env (no env shared across devs).
    * ``modal_env_strategy=SHARED`` -> apps deploy into the tier's
      stable Modal env from ``deploy.toml``'s ``modal_env`` field.
    * ``writes_local_state=true`` -> write ``~/.minds-<name>/client.toml``
      + ``secrets.toml`` after a successful deploy; ``false`` -> write
      nothing local.
    * ``tracks_generation=true`` -> mint a fresh per-tier generation id
      on first deploy and thread it into the ``litellm-connector``
      Modal Secret (powers activate-time auto-wipe across developers).

    Steps, in order:

    1. (``creates_resources``) Ensure the Modal env, Neon project, and
       SuperTokens app exist. Pre-existing instances are adopted.
    2. (``tracks_generation``) Mint or look up the tier generation id.
    3. Compute every per-env Modal Secret value (tier-shared Vault values
       + per-env overrides -- DSNs from the just-provisioned / adopted
       Neon project, app id from SuperTokens, and the *computed*
       deployed-app URLs which are deterministic under the shortened
       app/function names).
    4. Push every Modal Secret named in ``deploy_config.secrets.services``.
    5. Deploy ``llm-<tier>`` and ``rsc-<tier>`` into the Modal env from
       step (1)/(2); assert the URL Modal returns matches the computed.
    6. (``writes_local_state``) Write ``~/.minds-<name>/client.toml`` +
       ``secrets.toml``, overwriting any existing.

    No inline rollback today: if any step fails mid-flight, partial
    cloud-side state is left untouched. Phase 5 introduces
    ``minds env recover`` to converge back to the pre-deploy state.
    """
    lifecycle = deploy_config.lifecycle
    modal_workspace = str(deploy_config.modal_workspace)
    tier_vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    modal_env = _resolve_modal_env(name=name, lifecycle=lifecycle, deploy_config=deploy_config)

    # Preflight: must run from within the monorepo (we shell out to
    # ``modal deploy`` with absolute paths derived from this checkout).
    # Checked BEFORE we mint a deploy id, log it, or touch any
    # external state. (The CLI runs this check earlier too so vault
    # reads also gate behind it; here is the defense-in-depth for
    # non-CLI callers.)
    repo_root = find_monorepo_root()

    # Hold the per-env deploy lock for the rest of this function. Two
    # concurrent ``minds env deploy``s against the SAME env serialize
    # here; against different envs they each take their own lock and
    # proceed in parallel. See ``hold_deploy_lock`` for details.
    with hold_deploy_lock(repo_root=repo_root, env_name=str(name)):
        return _deploy_env_locked(
            name=name,
            tier=tier,
            deploy_config=deploy_config,
            credentials=credentials,
            providers=providers,
            parent_concurrency_group=parent_concurrency_group,
            repo_root=repo_root,
            lifecycle=lifecycle,
            modal_workspace=modal_workspace,
            tier_vault_prefix=tier_vault_prefix,
            modal_env=modal_env,
            explicit_strategy=explicit_strategy,
        )


def _deploy_env_locked(
    *,
    name: DevEnvName,
    tier: str,
    deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
    repo_root: Path,
    lifecycle: DeployLifecycleConfig,
    modal_workspace: str,
    tier_vault_prefix: str,
    modal_env: str,
    explicit_strategy: DeployStrategy | None,
) -> DeployedEnv:
    """The body of :func:`deploy_env`, run inside the per-env flock."""
    # Refuse-on-stale-recover-target check now happens INSIDE the lock
    # so two concurrent deploys against the same env can't both pass
    # (the second blocks on the flock until the first finishes,
    # whether it succeeded -- file gone -- or failed -- file present).
    if recover_target_exists(repo_root=repo_root, env_name=str(name)):
        raise MindError(
            f"Recover-target file exists at {recover_target_path(repo_root=repo_root, env_name=str(name))}; "
            "refusing to start a new deploy until the prior failed deploy is recovered. "
            "Run `minds env recover` first."
        )

    # Mint a fresh deploy id (UTC ISO-compact timestamp). Used as the
    # suffix on every Modal Secret pushed below and threaded into the
    # deployed Modal app's env so it pins to the matching Secret set.
    # Done AFTER preflight so a refuse-on-monorepo-check or refuse-on-
    # recover-target failure doesn't log a misleading "Deploy id..."
    # line for a deploy that's about to abort.
    deploy_id = make_deploy_id()
    logger.info("Deploy id for env {!r}: {}", str(name), deploy_id)

    # Step 1: provider creation -- only when this tier owns the resources.
    neon_record: NeonProjectRecord | None = None
    supertokens_record: SuperTokensAppRecord | None = None
    if lifecycle.creates_resources:
        with info_span("Ensuring Modal environment {!r}", str(name)):
            providers.ensure_modal_env(name, parent_concurrency_group)

        with info_span("Ensuring Neon project for {!r}", str(name)):
            neon_record = providers.create_neon_project(
                name, credentials.neon_org_id, credentials.neon_api_token, parent_concurrency_group
            )

        with info_span("Ensuring SuperTokens app for {!r}", str(name)):
            supertokens_record = providers.create_supertokens_app(
                name,
                credentials.supertokens_core_url,
                credentials.supertokens_api_key,
            )

    # Capture pre-deploy Modal app versions BEFORE any further mutation
    # so the recover-target carries them. None for never-deployed apps
    # (first-ever deploy of this env / tier) -- recover skips rollback
    # for those.
    app_names_to_capture = (f"llm-{tier}", f"rsc-{tier}")
    app_versions_to_restore = {
        app_name: providers.get_modal_app_latest_version(app_name, modal_env, parent_concurrency_group)
        for app_name in app_names_to_capture
    }
    logger.info("Captured pre-deploy app versions: {}", app_versions_to_restore)

    # Resolve which Neon project + branch to snapshot. For every tier:
    # - creates_resources=true (dev): use the just-provisioned project + branch
    # - creates_resources=false (staging/prod): use the operator-managed
    #   project from ``credentials.neon_project_id`` and look up the
    #   default branch via the Neon API.
    if lifecycle.creates_resources and neon_record is not None:
        neon_project_id_for_snapshot = neon_record.project_id
        neon_branch_id_for_snapshot = neon_record.branch_id
    elif credentials.neon_project_id is not None:
        neon_project_id_for_snapshot = credentials.neon_project_id
        with info_span("Resolving default branch on Neon project {!r}", neon_project_id_for_snapshot):
            neon_branch_id_for_snapshot = providers.resolve_neon_default_branch_id(
                neon_project_id_for_snapshot, credentials.neon_api_token
            )
    else:
        raise MindError(
            f"Tier {tier!r} has creates_resources=false but no NEON_PROJECT_ID in Vault. "
            f"Pre-deploy snapshot + recover-time DB restore both require it; refusing to "
            "ship a deploy that can't be rolled back. Populate "
            f"secrets/minds/{tier}/neon-admin/NEON_PROJECT_ID with the project id from the "
            "Neon console and re-run."
        )

    # F2: verify the Neon token can read the project the upcoming
    # snapshot + restore calls will target. Catches a stale or
    # misconfigured token at the cheapest possible probe
    # (``GET /projects/{id}``) BEFORE we create the snapshot branch or
    # mutate anything else. Without this, a token without read access
    # would only surface at recover time -- after the deploy had
    # already mutated other state and the operator was relying on
    # recover to roll it back.
    with info_span("Preflight: verifying Neon token can read project {!r}", neon_project_id_for_snapshot):
        providers.verify_neon_token_has_restore_scope(neon_project_id_for_snapshot, credentials.neon_api_token)

    # Snapshot the Neon project's default branch by creating a child
    # branch off of it. On rollback, recover does an instant restore
    # of the parent from this snapshot. Both DBs on the branch (host_pool
    # + litellm_cost) come back atomically.
    snapshot_name = make_neon_snapshot_branch_name(deploy_id)
    with info_span(
        "Creating Neon snapshot branch {!r} off project {} branch {}",
        snapshot_name,
        neon_project_id_for_snapshot,
        neon_branch_id_for_snapshot,
    ):
        neon_snapshot_branch_id = providers.create_neon_snapshot_branch(
            neon_project_id_for_snapshot,
            neon_branch_id_for_snapshot,
            snapshot_name,
            credentials.neon_api_token,
        )

    # F4: the snapshot branch now exists in Neon; if writing the
    # recover-target file fails (disk full, permission denied, fsync
    # error, ENOSPC, etc.), the snapshot is orphaned -- no file
    # points at it, so the operator has no ``minds env recover`` path
    # to clean it up. Best-effort delete the branch before re-raising
    # so a local-write failure doesn't compound into "and now there's
    # an orphan Neon branch I have to find + delete manually."
    recover_target = RecoverTarget(
        deploy_id=deploy_id,
        env_name=str(name),
        tier=tier,
        modal_env=modal_env,
        modal_workspace=modal_workspace,
        vault_path_prefix=tier_vault_prefix,
        neon_project_id=neon_project_id_for_snapshot,
        neon_branch_id=neon_branch_id_for_snapshot,
        neon_snapshot_branch_id=neon_snapshot_branch_id,
        app_versions_to_restore=app_versions_to_restore,
    )
    try:
        with info_span("Writing recover-target file at monorepo root"):
            write_recover_target_atomic(recover_target, repo_root=repo_root)
    except (OSError, MindError):
        try:
            providers.delete_neon_branch(
                neon_project_id_for_snapshot, neon_snapshot_branch_id, credentials.neon_api_token
            )
        except MindError as cleanup_exc:
            logger.warning(
                "Recover-target file write failed AND best-effort cleanup of the just-created "
                "Neon snapshot branch {!r} in project {!r} also failed: {} -- the branch is "
                "orphaned and must be deleted manually via the Neon console.",
                neon_snapshot_branch_id,
                neon_project_id_for_snapshot,
                cleanup_exc,
            )
        raise

    # F1: migrations run AFTER snapshot + recover-target file write so
    # ``minds env recover`` can restore the pre-migration state on
    # failure. With the snapshot in hand and the recover-target file
    # on disk, a failed (or merely partially-applied) migration is
    # rolled back along with any other deploy state. The earlier
    # pre-snapshot ordering would have left a failed migration
    # silently applied to the DB with no recover-target file to undo
    # it -- especially bad for shared tiers where the DB is
    # operator-managed and likely has live traffic.
    #
    # Runs for EVERY tier (dev + shared) so a new ``.sql`` file
    # shipped via PR applies to staging / production on their next
    # deploy, not just to dev. The provider implementation wraps
    # :func:`apply_pool_hosts_migrations` from ``migrations.py``,
    # which uses the schema_migrations tracking table so repeated
    # deploys only run new migrations.
    # - dev tier (creates_resources=true): targets ``neon_record.host_pool_dsn``,
    #   the per-env host_pool DB this deploy just (re-)created or adopted.
    # - shared tier (creates_resources=false): targets ``DATABASE_URL`` from the
    #   operator-managed ``secrets/minds/<tier>/neon`` Vault entry (single
    #   shared DB; the schema_migrations table lives alongside the
    #   pool_hosts table in that DB).
    host_pool_dsn = _resolve_host_pool_dsn_for_migrations(
        lifecycle=lifecycle,
        neon_record=neon_record,
        tier_vault_prefix=tier_vault_prefix,
        providers=providers,
        parent_concurrency_group=parent_concurrency_group,
    )
    with info_span("Applying pool-hosts schema migrations to host_pool"):
        applied = providers.apply_pool_hosts_migrations(host_pool_dsn, parent_concurrency_group)
        if applied:
            logger.info("Applied {} pool-hosts migration(s): {}", len(applied), [m.name for m in applied])

    # Seed the tier's default paid domains/emails (seed-if-absent) now that the
    # paid_domains / paid_emails tables exist. Runs every deploy for every tier;
    # idempotent and a no-op when the tier configures no defaults.
    paid_domains = tuple(str(d) for d in deploy_config.paid.domains)
    paid_emails = tuple(str(e) for e in deploy_config.paid.emails)
    if paid_domains or paid_emails:
        with info_span(
            "Seeding default paid-list entries (domains={}, emails={})", list(paid_domains), list(paid_emails)
        ):
            providers.seed_paid_list_defaults(host_pool_dsn, paid_domains, paid_emails, parent_concurrency_group)

    # Resolve the Modal deploy strategy now that we know whether a
    # migration ran. Done here (rather than at the CLI boundary) so the
    # decision sees the full deploy context the policy depends on; the
    # CLI only knows about ``--hard`` / ``--soft``.
    deploy_strategy = resolve_deploy_strategy(
        explicit=explicit_strategy,
        tier=tier,
        had_migrations=bool(applied),
    )
    logger.info(
        "Modal deploy strategy for env {!r} (tier {!r}, had_migrations={}): {}",
        str(name),
        tier,
        bool(applied),
        deploy_strategy.value,
    )

    # Step 2: tier generation id -- only when this tier exposes one.
    generation_id: str | None = None
    if lifecycle.tracks_generation:
        generation_id = providers.ensure_generation_id(tier_vault_prefix, parent_concurrency_group)
        logger.info("Tier {!r} generation id: {}", tier, generation_id)

    # Step 3+4: push every per-env Modal Secret. Single pass -- the
    # shortened app + function names keep the natural Modal hostname
    # under DNS's 63-char limit so the computed URLs in the per-env
    # overrides are exactly the URLs Modal will assign. Defensive
    # URL-match assertions after each ``modal deploy`` below catch any
    # future scheme change.
    services = tuple(str(s) for s in deploy_config.secrets.services)
    expected_litellm_proxy_url = _expected_litellm_proxy_url(
        name=name, lifecycle=lifecycle, tier=tier, modal_workspace=modal_workspace
    )
    expected_connector_url = _expected_connector_url(
        name=name, lifecycle=lifecycle, tier=tier, modal_workspace=modal_workspace
    )
    first_pass_overrides = _compute_secret_overrides(
        name=name,
        lifecycle=lifecycle,
        neon_record=neon_record,
        supertokens_record=supertokens_record,
        expected_connector_url=expected_connector_url,
        expected_litellm_proxy_url=expected_litellm_proxy_url,
    )
    litellm_master_key = _read_litellm_master_key(tier_vault_prefix, providers, parent_concurrency_group)
    with info_span("Pushing per-env Modal Secrets into env {!r}", modal_env):
        # Vault-backed services: one Modal Secret per entry in
        # ``[secrets].services``, base values from Vault + per-service
        # overrides from ``_compute_secret_overrides``.
        for service in services:
            per_service_overrides = dict(first_pass_overrides.get(service, {}))
            secret_name = timestamped_secret_name(service, tier, deploy_id)
            with info_span("Pushing per-env Modal Secret {!r}", secret_name):
                values = providers.read_per_env_secret_values(
                    service,
                    tier_vault_prefix,
                    per_service_overrides,
                    parent_concurrency_group,
                )
                providers.push_per_env_modal_secret(
                    secret_name,
                    values,
                    modal_env,
                    parent_concurrency_group,
                )
        # The ``litellm-connector`` Modal Secret is NOT vault-backed
        # (there is no ``secrets/minds/<tier>/litellm-connector`` Vault
        # entry); its values are 100% deploy-time computed. Push it
        # as a separately-named step so the user-facing
        # ``[secrets].services`` list stays "vault-backed only" and
        # we don't need a "skip-the-Vault-read" carve-out for this
        # one entry. The GC at ``gc_old_per_tier_secrets`` picks it up
        # by suffix-match alongside every other ``<svc>-<tier>-<id>``
        # secret, so no special GC bookkeeping is required.
        connector_secret_overrides: dict[str, str] = dict(first_pass_overrides.get("litellm-connector", {}))
        if litellm_master_key:
            connector_secret_overrides.setdefault("LITELLM_MASTER_KEY", litellm_master_key)
        if generation_id is not None:
            connector_secret_overrides.setdefault(GENERATION_ID_KEY, generation_id)
        connector_secret_overrides.setdefault(MINDS_ENV_NAME_KEY, str(name))
        # When the operator sets MINDS_INJECT_BROKEN_HEALTHCHECK at deploy time,
        # propagate it into the deployed connector's Modal Secret so the
        # in-container healthcheck returns 500 and the auto-rollback path
        # fires. Used by `test_deploy_rollback` to drive that flow; unset
        # in every real deploy.
        broken_healthcheck = os.environ.get(INJECT_BROKEN_HEALTHCHECK_ENV_VAR)
        if broken_healthcheck:
            connector_secret_overrides.setdefault(INJECT_BROKEN_HEALTHCHECK_ENV_VAR, broken_healthcheck)
        connector_secret_name = timestamped_secret_name("litellm-connector", tier, deploy_id)
        with info_span("Pushing derived Modal Secret {!r}", connector_secret_name):
            providers.push_per_env_modal_secret(
                connector_secret_name,
                # Strip out empty values to mirror ``build_per_env_secret_values``'s
                # filter; Modal rejects empty-string values.
                {k: v for k, v in connector_secret_overrides.items() if v},
                modal_env,
                parent_concurrency_group,
            )

    # Step 5: modal deploys.
    litellm_proxy_min_containers = int(deploy_config.min_containers.litellm_proxy)
    connector_min_containers = int(deploy_config.min_containers.connector)
    litellm_proxy_scaledown_window = int(deploy_config.scaledown_window.litellm_proxy)
    connector_scaledown_window = int(deploy_config.scaledown_window.connector)

    with info_span(
        "Deploying llm-{} into env {!r} (min_containers={}, scaledown_window={}, strategy={})",
        tier,
        modal_env,
        litellm_proxy_min_containers,
        litellm_proxy_scaledown_window,
        deploy_strategy.value,
    ):
        litellm_proxy_url = providers.deploy_litellm_proxy(
            modal_env,
            tier,
            litellm_proxy_min_containers,
            litellm_proxy_scaledown_window,
            deploy_id,
            deploy_strategy,
            parent_concurrency_group,
        )
    _assert_deploy_url_matches(actual=litellm_proxy_url, expected=expected_litellm_proxy_url, app=f"llm-{tier}")

    with info_span(
        "Deploying rsc-{} into env {!r} (min_containers={}, scaledown_window={}, strategy={})",
        tier,
        modal_env,
        connector_min_containers,
        connector_scaledown_window,
        deploy_strategy.value,
    ):
        connector_url = providers.deploy_remote_service_connector(
            modal_env,
            tier,
            connector_min_containers,
            connector_scaledown_window,
            deploy_id,
            deploy_strategy,
            parent_concurrency_group,
        )
    _assert_deploy_url_matches(actual=connector_url, expected=expected_connector_url, app=f"rsc-{tier}")

    # Step 6a: health check -- poll both apps' health endpoints until
    # they return 200. Failure raises ``HealthCheckFailedError`` which
    # the CLI surfaces with the same "run `minds env recover`" guidance
    # as any other deploy failure. The recover-target file is still on
    # disk at this point so recover will roll back both apps + Neon.
    with info_span("Health check: polling both apps for 200"):
        providers.await_apps_healthy(connector_url, litellm_proxy_url)

    # Step 6b: local state (only for tiers that write it).
    client_config_path: str | None = None
    secrets_path: str | None = None
    if lifecycle.writes_local_state:
        # Both records are guaranteed populated here: the
        # ``DeployLifecycleConfig`` model validator rejects
        # ``writes_local_state=true`` + ``creates_resources=false`` at
        # deploy.toml parse time, and ``creates_resources=true`` is the
        # only branch that populates ``neon_record`` / ``supertokens_record``.
        # The asserts are defense-in-depth for callers that bypass the
        # parser (tests etc.).
        assert neon_record is not None, (
            "writes_local_state implies creates_resources (see DeployLifecycleConfig validator)"
        )
        assert supertokens_record is not None, (
            "writes_local_state implies creates_resources (see DeployLifecycleConfig validator)"
        )
        public_config = ClientEnvConfig(connector_url=connector_url, litellm_proxy_url=litellm_proxy_url)
        client_config_path = str(write_client_config(public_config, name=name))
        secrets_path = str(
            write_secrets_file(
                {
                    "NEON_HOST_POOL_DSN": neon_record.host_pool_dsn,
                    "NEON_LITELLM_DSN": neon_record.litellm_cost_dsn,
                    "SUPERTOKENS_CONNECTION_URI": SecretStr(supertokens_record.connection_uri),
                    "SUPERTOKENS_API_KEY": supertokens_record.api_key,
                },
                name=name,
            )
        )

    # Deploy reached its happy path: delete the snapshot branch (best-
    # effort: keeping it around just clutters the project), then delete
    # the recover-target file. On any failure before this point the
    # snapshot branch + file stay so recover can use them.
    if neon_snapshot_branch_id and neon_project_id_for_snapshot:
        with info_span("Deleting Neon snapshot branch {!r} after successful deploy", neon_snapshot_branch_id):
            try:
                providers.delete_neon_branch(
                    neon_project_id_for_snapshot, neon_snapshot_branch_id, credentials.neon_api_token
                )
            except MindError as exc:
                logger.warning(
                    "Failed to delete Neon snapshot branch {!r} after successful deploy: {} -- "
                    "leaving in place (operator can delete manually via the Neon console).",
                    neon_snapshot_branch_id,
                    exc,
                )
    with info_span("Deleting recover-target file after successful deploy"):
        delete_recover_target(repo_root=repo_root, env_name=str(name))

    # GC old timestamped Modal Secrets at the end of every successful
    # deploy. Best-effort -- failures here are logged but never re-raise
    # (we don't want a noisy Modal API to mark the whole deploy failed).
    with info_span("GC: keeping last {} Modal Secrets per <service>-{} in env {!r}", 10, tier, modal_env):
        try:
            gc_old_per_tier_secrets(
                modal_env=modal_env,
                tier=tier,
                list_modal_secrets_fn=providers.list_modal_secrets,
                delete_modal_secret_fn=providers.delete_modal_secret,
                keep_last=10,
                parent_cg=parent_concurrency_group,
            )
        except ModalDeployError as exc:
            logger.warning(
                "GC of old timestamped Modal Secrets failed in env {!r}: {} -- ignoring (deploy succeeded)",
                modal_env,
                exc,
            )

    return DeployedEnv(
        name=name,
        tier=tier,
        modal_env=modal_env,
        connector_url=connector_url,
        litellm_proxy_url=litellm_proxy_url,
        client_config_path=client_config_path,
        secrets_path=secrets_path,
    )


def _resolve_modal_env(*, name: DevEnvName, lifecycle: DeployLifecycleConfig, deploy_config: DeployEnvConfig) -> str:
    """Pick the Modal env name based on ``[lifecycle].modal_env_strategy``."""
    match lifecycle.modal_env_strategy:
        case ModalEnvStrategy.PER_ENV:
            return str(name)
        case ModalEnvStrategy.SHARED:
            return str(deploy_config.modal_env)
        case _ as unreachable:
            assert_never(unreachable)


def _resolve_host_pool_dsn_for_migrations(
    *,
    lifecycle: DeployLifecycleConfig,
    neon_record: NeonProjectRecord | None,
    tier_vault_prefix: str,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
) -> SecretStr:
    """Return the DSN ``apply_pool_hosts_migrations`` should target for this tier.

    Per-env tiers (dev / ci, ``creates_resources=true``): use the per-env
    host_pool DSN from the freshly-created or adopted Neon project
    record. Shared tier (``creates_resources=false``): read
    ``DATABASE_URL`` from the operator-managed
    ``secrets/minds/<tier>/neon`` Vault entry (single shared DB where
    pool_hosts + litellm tables co-exist).

    Raises :class:`MindError` if the shared-tier Vault entry is missing
    or lacks ``DATABASE_URL`` -- we'd otherwise silently skip migrations
    on shared tiers, which was the F17 bug we're fixing.
    """
    if lifecycle.creates_resources:
        assert neon_record is not None, "creates_resources=true should have populated neon_record"
        return neon_record.host_pool_dsn

    neon_vault_values = providers.read_per_env_secret_values(
        "neon",
        tier_vault_prefix,
        {},
        parent_concurrency_group,
    )
    database_url = neon_vault_values.get("DATABASE_URL", "")
    if not database_url:
        raise MindError(
            f"Cannot apply pool-hosts migrations: shared-tier Vault entry "
            f"{tier_vault_prefix}/neon does not have a non-empty DATABASE_URL. "
            "The host_pool schema migrations need a target DB; populate the Vault "
            "entry with the tier's Neon DSN and re-run."
        )
    return SecretStr(database_url)


def _expected_connector_url(
    *, name: DevEnvName, lifecycle: DeployLifecycleConfig, tier: str, modal_workspace: str
) -> AnyUrl:
    match lifecycle.modal_env_strategy:
        case ModalEnvStrategy.PER_ENV:
            return per_env_connector_url(name, modal_workspace, tier=tier)
        case ModalEnvStrategy.SHARED:
            return tier_connector_url(tier, modal_workspace)
        case _ as unreachable:
            assert_never(unreachable)


def _expected_litellm_proxy_url(
    *, name: DevEnvName, lifecycle: DeployLifecycleConfig, tier: str, modal_workspace: str
) -> AnyUrl:
    match lifecycle.modal_env_strategy:
        case ModalEnvStrategy.PER_ENV:
            return per_env_litellm_proxy_url(name, modal_workspace, tier=tier)
        case ModalEnvStrategy.SHARED:
            return tier_litellm_proxy_url(tier, modal_workspace)
        case _ as unreachable:
            assert_never(unreachable)


def _compute_secret_overrides(
    *,
    name: DevEnvName,
    lifecycle: DeployLifecycleConfig,
    neon_record: NeonProjectRecord | None,
    supertokens_record: SuperTokensAppRecord | None,
    expected_connector_url: AnyUrl,
    expected_litellm_proxy_url: AnyUrl,
) -> dict[str, dict[str, str]]:
    """Build the per-service Modal Secret override dict for one deploy.

    For ``creates_resources=true`` tiers the overrides include per-env
    Neon DSNs + the per-env SuperTokens connection URI; the URLs come
    from the computed-up-front values. For ``creates_resources=false``
    tiers the operator's Vault entries already hold the DSNs +
    connection URI -- we only inject the URL-dependent values.
    """
    overrides: dict[str, dict[str, str]] = {
        "supertokens": {"AUTH_WEBSITE_DOMAIN": str(expected_connector_url)},
        "litellm-connector": {"LITELLM_PROXY_URL": str(expected_litellm_proxy_url)},
    }
    if lifecycle.creates_resources:
        assert neon_record is not None
        assert supertokens_record is not None
        overrides["supertokens"]["SUPERTOKENS_CONNECTION_URI"] = supertokens_record.connection_uri
        overrides["neon"] = {"DATABASE_URL": neon_record.host_pool_dsn.get_secret_value()}
        overrides["litellm"] = {"DATABASE_URL": neon_record.litellm_cost_dsn.get_secret_value()}
    return overrides


def destroy_env(
    name: DevEnvName,
    *,
    tier: str,
    deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
    keep_agents: bool = False,
) -> None:
    """Tear down everything ``deploy`` created for env ``name`` and remove the env root.

    Single destroy function for every env type (dev, staging, anything
    else that follows the per-env-data-roots pattern). The flow is
    shared end to end -- only the *implementation* of a few cleanup
    steps differs by tier because resource ownership does:

    * For the ``dev`` tier, ``deploy`` creates the per-env Modal
      environment, Neon DB, and SuperTokens app outright. So destroy
      *deletes* them (cascade-clears their contents).
    * For shared tiers (``staging`` today, anything similar later),
      ``deploy`` does NOT create those resources -- they're operator-
      managed via Vault. Destroy clears the *data inside them*
      (``modal app stop`` + ``modal secret delete``, SuperTokens user
      wipe via delete + recreate of the same ``app_id``, Neon
      ``DROP SCHEMA public CASCADE``) but leaves the resources
      themselves intact so the operator's Vault entries stay valid.
    * Generation-id removal is the *only* outright difference:
      shared tiers track a per-tier generation id in Vault that powers
      ``activate``-time auto-wipe across developers; dev doesn't.
      Driven by ``deploy_config.lifecycle.tracks_generation``.

    Steps, in order, for every env type:

    1. ``mngr destroy`` every agent under ``~/.minds-<name>/mngr/agents/``
       so their cloud resources (Docker containers, pool hosts,
       Cloudflare tunnels) stop cleanly before being torn down.
       Skipped when ``keep_agents=True``.
    2. Delete every OVH VPS tagged ``minds_env=<name>``.
    3. Enumerate + delete every Cloudflare tunnel with
       ``metadata.env=<name>`` (filtered by env name; the tag the
       connector sets at create time encodes the owning env, not the
       tier).
    4. Clear SuperTokens app data (tier-dependent: delete the app
       outright for dev / wipe its users for shared tiers).
    5. Clear Neon DB data (tier-dependent: delete the DB outright for
       dev / DROP SCHEMA for shared tiers).
    6. Clear Modal infra (tier-dependent: delete the Modal env outright
       for dev / stop apps + delete secrets for shared tiers).
    7. For shared tiers only: delete the tier generation id from Vault
       so the next deploy mints a fresh one + every dev's next
       ``activate`` sees a mismatch and auto-wipes their local state.
    8. Finally, remove ``~/.minds-<name>/`` -- ONLY if every prior step
       succeeded. On any failure, the env root stays so the operator
       can re-run ``destroy`` to pick up where things broke (rather
       than silently leaking expensive cloud resources because the
       local pointer is gone).

    Proceeds even when the env root is missing on disk. The local env
    root is a convenience pointer; the cloud-side resources are keyed
    off the env *name* (Modal env, Neon project, SuperTokens app,
    Cloudflare tunnel tags, OVH IAM tags), all of which we can clean
    up by name without needing the local directory. This makes destroy
    safe to re-run after an operator who manually ``rm -rf``'d the env
    root would otherwise be locked out of the cloud cleanup.
    """
    env_root_was_present = env_root_exists(name)
    if not env_root_was_present:
        logger.warning(
            "No env root for env {!r} at {}; proceeding with cloud-side cleanup based on the env name. "
            "Local state is keyed off the directory, but the cloud resources are keyed off the name -- "
            "every step below can converge on a missing-local-root env. mngr-agent destroy (step 1) will "
            "be a no-op since there's nothing under ``mngr/agents/`` to walk.",
            str(name),
            env_root_dir(name),
        )

    lifecycle = deploy_config.lifecycle
    tier_vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    modal_env_for_tier_ops = _resolve_modal_env(name=name, lifecycle=lifecycle, deploy_config=deploy_config)

    # Step 1: mngr agents first, so their docker containers / pool
    # hosts / tunnels stop cleanly before we tear down the cloud
    # resources they reference.
    if keep_agents:
        logger.warning(
            "minds env destroy {!r}: --keep-agents passed; skipping mngr-agent teardown "
            "(and the Docker state-container cleanup, since kept agents still rely on it). "
            "Run `mngr destroy <agent>` manually for any agents bound to this env.",
            str(name),
        )
    else:
        with info_span("Destroying mngr agents under env {!r}", str(name)):
            destroyed_count = destroy_all_mngr_agents_in_env(
                name,
                destroy_agents=providers.destroy_mngr_agents,
                parent_concurrency_group=parent_concurrency_group,
            )
            if destroyed_count:
                logger.info("Destroyed {} mngr agent(s) under env {!r}", destroyed_count, str(name))

        # Step 1b: remove the env's mngr Docker state container + backing
        # volume. The singleton state container is independent of individual
        # agents (it is not torn down by `mngr destroy`), so it must be removed
        # explicitly -- before the env root, and the profile it derives user_id
        # from, are gone. Skipped under keep_agents since kept agents need it.
        with info_span("Cleaning up Docker state container for env {!r}", str(name)):
            providers.cleanup_state_container(name, parent_concurrency_group)

    # Step 2: OVH VPSes tagged with this env.
    with info_span("Cleaning up OVH VPSes tagged for env {!r}", str(name)):
        ovh_instances = providers.list_ovh_instances(name, credentials.ovh_credentials)
        if ovh_instances:
            providers.delete_ovh_instances(ovh_instances, credentials.ovh_credentials)
            logger.info("Deleted {} OVH VPS(es) for env {!r}", len(ovh_instances), str(name))

    # Step 3: Cloudflare tunnels tagged with this env. Keyed off env
    # NAME (not tier), since dev envs share the dev-tier CF account and
    # we want to find only this specific env's tunnels.
    with info_span("Cleaning up Cloudflare tunnels tagged for env {!r}", str(name)):
        cf_vault_values = providers.read_per_env_secret_values(
            "cloudflare",
            tier_vault_prefix,
            {},
            parent_concurrency_group,
        )
        deleted_tunnels = _cleanup_cloudflare_tunnels_for_env(
            name, cloudflare_vault_values=cf_vault_values, providers=providers
        )
        if deleted_tunnels:
            logger.info("Deleted {} Cloudflare tunnel(s) for env {!r}", deleted_tunnels, str(name))

    # Step 4: SuperTokens (dev deletes the per-env app outright; shared
    # tiers wipe users via delete + recreate of the same app id).
    if lifecycle.creates_resources:
        with info_span("Deleting SuperTokens app for env {!r}", str(name)):
            providers.delete_supertokens_app(
                name,
                credentials.supertokens_core_url,
                credentials.supertokens_api_key,
            )
    else:
        with info_span("Wiping SuperTokens app data for env {!r}", str(name)):
            supertokens_values = providers.read_per_env_secret_values(
                "supertokens",
                tier_vault_prefix,
                {},
                parent_concurrency_group,
            )
            _wipe_supertokens_for_tier(supertokens_values, providers=providers, tier=tier)

    # Step 5: Neon (dev deletes the per-env *project* outright -- atomic
    # teardown of both DBs + roles + endpoints; shared tiers DROP SCHEMA
    # on the operator-managed DB they keep across destroy/redeploy).
    if lifecycle.creates_resources:
        with info_span("Deleting Neon project for env {!r}", str(name)):
            providers.delete_neon_project(name, credentials.neon_org_id, credentials.neon_api_token)
    else:
        with info_span("Wiping Neon DB schema for env {!r}", str(name)):
            neon_values = providers.read_per_env_secret_values(
                "neon",
                tier_vault_prefix,
                {},
                parent_concurrency_group,
            )
            _wipe_neon_for_tier(neon_values, providers=providers, tier=tier, parent_cg=parent_concurrency_group)

    # Step 6: Modal (dev deletes the per-env Modal env outright which
    # cascade-deletes its apps / secrets / volumes; shared tiers stop
    # the deployed apps + delete per-tier Modal Secrets so the next
    # deploy re-pushes fresh values from Vault).
    if lifecycle.creates_resources:
        with info_span("Deleting Modal environment {!r} (cascade-deletes apps + secrets)", str(name)):
            providers.delete_modal_env(name, parent_concurrency_group)
    else:
        with info_span("Stopping Modal apps for env {!r} in Modal env {!r}", str(name), modal_env_for_tier_ops):
            for app_name in (f"llm-{tier}", f"rsc-{tier}"):
                providers.stop_modal_app(app_name, modal_env_for_tier_ops, parent_concurrency_group)
        # Delete every timestamped Modal Secret matching ``<svc>-<tier>-*``
        # in the tier's Modal env. Re-uses the same GC helper as deploy,
        # with ``keep_last=0`` to drop the whole set.
        with info_span("Deleting all timestamped per-tier Modal Secrets in env {!r}", modal_env_for_tier_ops):
            gc_old_per_tier_secrets(
                modal_env=modal_env_for_tier_ops,
                tier=tier,
                list_modal_secrets_fn=providers.list_modal_secrets,
                delete_modal_secret_fn=providers.delete_modal_secret,
                keep_last=0,
                parent_cg=parent_concurrency_group,
            )

    # Step 7: generation id removal -- ONLY for tiers that use generation
    # tracking (driven by ``deploy_config.lifecycle.tracks_generation``).
    # For dev, there is no generation Vault entry to remove. Production
    # destroy is hard-refused at the CLI today, so this path is only
    # actually reached for ``staging``.
    if lifecycle.tracks_generation:
        with info_span("Deleting tier {!r} generation id from Vault", tier):
            providers.delete_generation_id(tier_vault_prefix, parent_concurrency_group)

    # Step 8: env root removal LAST, only on full success.
    delete_env_root(name)


def _wipe_supertokens_for_tier(
    supertokens_vault_values: dict[str, str],
    *,
    providers: Providers,
    tier: str,
) -> None:
    """Pull the bits we need from the SuperTokens Vault entry + invoke the wipe.

    Surfaces a ``MindError`` if either the connection URI or the API
    key is missing from the Vault entry (which would mean the tier
    wasn't fully provisioned -- destroy shouldn't silently skip the
    wipe in that case).
    """
    connection_uri = supertokens_vault_values.get("SUPERTOKENS_CONNECTION_URI", "")
    api_key_str = supertokens_vault_values.get("SUPERTOKENS_API_KEY", "")
    if not connection_uri or not api_key_str:
        raise MindError(
            f"Cannot wipe SuperTokens app data for tier {tier!r}: Vault entry is missing "
            "SUPERTOKENS_CONNECTION_URI or SUPERTOKENS_API_KEY. Populate the entry "
            f"at secrets/minds/{tier}/supertokens (see .minds/template/supertokens.sh)."
        )
    # The connection URI is `<core_url>/appid-<app_id>`; the core URL
    # is everything up to the `/appid-` segment.
    app_id = app_id_from_connection_uri(connection_uri)
    core_base_url = connection_uri.rsplit(f"/appid-{app_id}", 1)[0]
    providers.wipe_supertokens_app_data(app_id, core_base_url, SecretStr(api_key_str))


def _cleanup_cloudflare_tunnels_for_env(
    name: DevEnvName,
    *,
    cloudflare_vault_values: dict[str, str],
    providers: Providers,
) -> int:
    """List + delete every Cloudflare tunnel whose metadata.env equals ``name``.

    Returns the count of tunnels deleted. Raises :class:`MindError`
    when the cloudflare Vault entry is missing the keys we need; the
    caller propagates so destroy aborts and the operator can fix
    Vault rather than silently leaking tunnels.
    """
    account_id = cloudflare_vault_values.get("CLOUDFLARE_ACCOUNT_ID", "")
    api_token = cloudflare_vault_values.get("CLOUDFLARE_API_TOKEN", "")
    if not account_id or not api_token:
        raise MindError(
            f"Cannot enumerate Cloudflare tunnels for env {str(name)!r}: cloudflare Vault entry "
            "is missing CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN."
        )
    tunnel_ids = providers.list_cloudflare_tunnels_for_env(name, account_id, SecretStr(api_token))
    if tunnel_ids:
        providers.delete_cloudflare_tunnels(tunnel_ids, account_id, SecretStr(api_token))
    return len(tunnel_ids)


def _wipe_neon_for_tier(
    neon_vault_values: dict[str, str],
    *,
    providers: Providers,
    tier: str,
    parent_cg: ConcurrencyGroup,
) -> None:
    """Pull DATABASE_URL out of the Neon Vault entry + invoke the schema wipe."""
    dsn_str = neon_vault_values.get("DATABASE_URL", "")
    if not dsn_str:
        raise MindError(
            f"Cannot wipe Neon DB schema for tier {tier!r}: Vault entry is missing "
            f"DATABASE_URL. Populate the entry at secrets/minds/{tier}/neon "
            "(see .minds/template/neon.sh)."
        )
    providers.wipe_neon_db_schema(SecretStr(dsn_str), parent_cg)


def _read_litellm_master_key(
    tier_vault_prefix: str,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
) -> str:
    """Pull ``LITELLM_MASTER_KEY`` out of the tier-shared ``litellm`` Vault entry.

    The connector's ``litellm-connector`` Modal Secret needs the same
    master key the proxy uses (so the connector's ``/keys/*`` route can
    mint virtual keys against the proxy's admin API). Returning empty
    string when the Vault entry isn't populated lets the caller skip the
    override instead of writing an empty value.
    """
    values = providers.read_per_env_secret_values("litellm", tier_vault_prefix, {}, parent_concurrency_group)
    return values.get("LITELLM_MASTER_KEY", "")


def _assert_deploy_url_matches(*, actual: AnyUrl, expected: AnyUrl, app: str) -> None:
    """Assert ``modal deploy`` reported the URL we computed up front.

    Under the shortened app + function names the natural Modal hostname
    always fits under DNS's 63-char limit, so Modal's URL is exactly
    what ``per_env_*_url`` / ``tier_*_url`` predict. A mismatch means
    either we miscomputed (bug) or Modal changed its URL scheme on us
    (real-world signal we need to know about immediately). Raise a
    ``ModalDeployError`` so the deploy fails loudly rather than
    silently shipping the wrong URLs into the per-env secrets.

    Strips any trailing slash that either side may have appended so a
    cosmetic difference doesn't trip the check.
    """
    actual_str = str(actual).rstrip("/")
    expected_str = str(expected).rstrip("/")
    if actual_str != expected_str:
        raise ModalDeployError(
            f"`modal deploy` URL mismatch for {app!r}: "
            f"computed {expected_str!r} but Modal reported {actual_str!r}. "
            "Either the URL formula in `per_env_deploy.py` is stale or Modal "
            "changed its hostname scheme; fix before continuing."
        )


def list_dev_envs() -> tuple[DevEnvSummary, ...]:
    """Return one :class:`DevEnvSummary` per ``~/.minds*/`` directory on disk.

    Globs the user's home for every env root, including ``~/.minds/``
    (production) and ``~/.minds-staging/`` if they exist. Each row
    carries the env name, the absolute env-root path, the resolved
    client.toml path (per-env file for dev envs, committed in-repo
    file for reserved tiers), where that client.toml lives
    (``client_config_source``), and the ``connector_url`` parsed out
    of it. Rows that have no client.toml anywhere (an unprovisioned
    dev env) leave ``connector_url`` / ``client_config_path`` /
    ``client_config_source`` as ``None``.
    """
    summaries: list[DevEnvSummary] = []
    for env_root in list_env_root_dirs():
        env_name = _env_name_from_root_path(env_root)
        client_path: Path | None = None
        client_config_source: str | None = None
        connector_url: AnyUrl | None = None
        if env_name in {"production", "staging"}:
            # Reserved tiers: client.toml is committed in-repo. Fall
            # back to the repo path so the list output doesn't
            # mislead the operator into thinking the tier is
            # unprovisioned.
            repo_path = repo_tier_client_config_path(env_name)
            if repo_path.is_file():
                client_path = repo_path
                client_config_source = "in_repo"
                try:
                    connector_url = load_client_config(repo_path).connector_url
                except EnvConfigError:
                    # Malformed in-repo file: leave connector_url None
                    # rather than blowing up the whole list. The CLI
                    # surfaces "no client.toml" which prompts the
                    # operator to fix the committed file.
                    pass
        else:
            dev_env_name = DevEnvName(env_name)
            if client_config_exists(dev_env_name):
                client_path = client_config_file(dev_env_name)
                client_config_source = "env_root"
                connector_url = read_client_config_file(dev_env_name).connector_url
        summaries.append(
            DevEnvSummary(
                name=env_name,
                env_root=str(env_root),
                client_config_path=str(client_path) if client_path is not None else None,
                client_config_source=client_config_source,
                connector_url=connector_url,
            )
        )
    return tuple(summaries)


def _env_name_from_root_path(env_root: Path) -> str:
    """Convert ``~/.minds/`` or ``~/.minds-<name>/`` back to an env name.

    The mirror of :func:`imbue.minds.envs.paths.env_root_dir`. Inlined
    here (instead of in ``paths.py``) so the regex stays close to its
    only caller, the ``list_dev_envs`` glob walker.
    """
    dirname = env_root.name
    if dirname == ".minds":
        return "production"
    assert dirname.startswith(".minds-"), f"Unexpected env root path: {env_root}"
    return dirname[len(".minds-") :]


# Re-export the per_env_deploy helpers so the CLI can build a Providers
# bundle without importing both modules.
__all__ = [
    "DeployedEnv",
    "DevEnvSummary",
    "ProviderCredentials",
    "Providers",
    "build_per_env_secret_values",
    "delete_modal_secret",
    "deploy_env",
    "deploy_litellm_proxy",
    "deploy_remote_service_connector",
    "destroy_env",
    "ensure_modal_env",
    "list_dev_envs",
    "push_per_env_modal_secret",
    "stop_modal_app",
]
