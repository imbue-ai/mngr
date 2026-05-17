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

from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import info_span
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.envs.generation import GENERATION_ID_KEY
from imbue.minds.envs.local_store import client_config_exists
from imbue.minds.envs.local_store import delete_env_root
from imbue.minds.envs.local_store import env_root_exists
from imbue.minds.envs.local_store import read_client_config_file
from imbue.minds.envs.local_store import write_client_config
from imbue.minds.envs.local_store import write_secrets_file
from imbue.minds.envs.mngr_agent_cleanup import DestroyMngrAgentFn
from imbue.minds.envs.mngr_agent_cleanup import destroy_all_mngr_agents_in_env
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.paths import list_env_root_dirs
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.per_env_deploy import build_per_env_secret_values
from imbue.minds.envs.per_env_deploy import compute_per_env_overrides
from imbue.minds.envs.per_env_deploy import delete_modal_secret
from imbue.minds.envs.per_env_deploy import deploy_litellm_proxy
from imbue.minds.envs.per_env_deploy import deploy_remote_service_connector
from imbue.minds.envs.per_env_deploy import ensure_modal_env
from imbue.minds.envs.per_env_deploy import per_env_connector_url
from imbue.minds.envs.per_env_deploy import per_env_litellm_proxy_url
from imbue.minds.envs.per_env_deploy import per_env_secret_services
from imbue.minds.envs.per_env_deploy import push_per_env_modal_secret
from imbue.minds.envs.per_env_deploy import stop_modal_app
from imbue.minds.envs.per_env_deploy import tier_connector_url
from imbue.minds.envs.per_env_deploy import tier_litellm_proxy_url
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import DevEnvProvisioningError
from imbue.minds.envs.providers.modal_env import ModalEnvProviderError
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.neon_db import NeonProviderError
from imbue.minds.envs.providers.ovh_tags import OvhCredentials
from imbue.minds.envs.providers.ovh_tags import OvhProviderError
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensProviderError
from imbue.minds.envs.providers.supertokens_app import app_id_from_connection_uri
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

# Tier names that are NOT eligible for generation-id tracking. The
# generation id is a "did this shared env get destroyed + redeployed
# since I last activated" signal -- it only matters for tiers where
# multiple developers share one deployment AND destroy is a real
# possibility. ``dev`` is per-developer (each dev controls their own
# destroy, no inter-dev coordination needed). ``production`` is
# hard-refused for destroy at the CLI layer, so there's no signal to
# track. Anything else (``staging`` today, future shared envs) gets
# generation tracking.
_TIERS_WITHOUT_GENERATION_TRACKING: Final[frozenset[str]] = frozenset({"dev", "production"})


def tier_uses_generation_tracking(tier: str) -> bool:
    """Return True iff this tier should mint + expose + auto-wipe on generation id."""
    return tier not in _TIERS_WITHOUT_GENERATION_TRACKING


_PROVIDER_ERRORS: tuple[type[Exception], ...] = (
    ModalEnvProviderError,
    NeonProviderError,
    SuperTokensProviderError,
    ModalDeployError,
    OvhProviderError,
    MindError,
)


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
            "``secrets/minds/dev/neon-admin.NEON_ORG_ID``."
        ),
    )
    neon_api_token: SecretStr = Field(
        description="Neon API token with project-create scope on the dev tier's Neon org.",
    )
    supertokens_core_url: str = Field(description="Dev-tier SuperTokens core base URL.")
    supertokens_api_key: SecretStr = Field(description="Dev-tier SuperTokens admin API key.")
    ovh_credentials: OvhCredentials = Field(description="Dev-tier OVH AK/AS/CK credentials (shared across dev envs).")


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
# (modal_env, tier, min_containers, cg) -> deployed URL. ``modal_env``
# is the dev env name for dev-tier deploys or the tier's stable Modal
# env (``main`` by convention) for staging / production deploys.
# ``min_containers`` is the per-app warm-pool size from the tier's
# ``[min_containers]`` deploy.toml block.
DeployModalAppFn = Callable[[str, str, int, ConcurrencyGroup], AnyUrl]
# (app_name, modal_env, cg) -> None. Used by tier destroys to ``modal
# app stop`` each deployed app. Idempotent in the underlying call.
StopModalAppFn = Callable[[str, str, ConcurrencyGroup], None]
# (secret_name, modal_env, cg) -> None. Used by tier destroys to
# ``modal secret delete`` each pushed per-tier Modal Secret. Idempotent.
DeleteModalSecretFn = Callable[[str, str, ConcurrencyGroup], None]
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
    destroy_mngr_agent: DestroyMngrAgentFn = Field(
        description=(
            "(agent_id, mngr_host_dir, mngr_prefix, cg) -> `mngr destroy <agent_id>` "
            "with the env's MNGR_* vars exported. Used before cloud teardown so the env's "
            "agents stop cleanly before their resources go away."
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


class DeployedDevEnv(FrozenModel):
    """Summary returned by :func:`deploy_dev_env`."""

    name: DevEnvName
    client_config_path: str = Field(description="Path to the ~/.minds-<name>/client.toml that was written.")
    secrets_path: str = Field(description="Path to the ~/.minds-<name>/secrets.toml that was written.")
    connector_url: AnyUrl
    litellm_proxy_url: AnyUrl


class DeployedTierEnv(FrozenModel):
    """Summary returned by :func:`deploy_tier_env`.

    Tier deploys write nothing to disk; this carries the URLs Modal
    reported back for logging only.
    """

    tier: str
    modal_env: str
    connector_url: AnyUrl
    litellm_proxy_url: AnyUrl


class DevEnvSummary(FrozenModel):
    """One row of :func:`list_dev_envs`.

    ``connector_url`` is None for env roots that have no parseable
    ``client.toml`` (e.g. a freshly-mkdir'd ``~/.minds-staging/`` whose
    URLs live in the in-repo file but haven't been activated against
    yet, or a partial deploy that failed before writing the file).
    """

    name: str = Field(description="The env name (e.g. 'dev-josh-3'), or 'production' for ~/.minds/.")
    env_root: str = Field(description="Absolute path to the env root directory on disk.")
    client_config_path: str | None = Field(
        default=None,
        description="Path to the per-env client.toml under env_root, or None if the file is absent.",
    )
    connector_url: AnyUrl | None = Field(
        default=None,
        description="connector_url parsed from the per-env client.toml, or None if no client.toml exists.",
    )


def deploy_dev_env(
    name: DevEnvName,
    *,
    tier: str,
    deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
) -> DeployedDevEnv:
    """Provision (or upgrade) the dev env named ``name``.

    Steps, in order:

    1. Ensure the Modal env named ``name`` exists (idempotent).
    2. Create or look up the per-dev-env Neon database.
    3. Create or look up the per-dev-env SuperTokens app.
    4. Push every per-env Modal Secret (tier-shared Vault values + per-env
       overrides; placeholder for Vault entries that aren't populated).
       The per-env overrides already carry the *computed* deployed-app
       URLs (``per_env_connector_url`` / ``per_env_litellm_proxy_url``);
       since the shortened app + function names keep the natural Modal
       hostname under DNS's 63-char limit, those computed URLs are
       exactly what Modal will assign at deploy time -- no second-pass
       re-push required.
    5. Deploy ``llm-<tier>`` into Modal env ``<name>``; assert the URL
       Modal returned matches the up-front computed value.
    6. Deploy ``rsc-<tier>`` into Modal env ``<name>``; same assertion.
    7. Write ``~/.minds-<name>/client.toml`` (mode 0644) + ``~/.minds-<name>/secrets.toml``
       (mode 0600), overwriting any existing.

    On any *provider creation* step (1-3) failing, the cleanup for
    previously-completed creation steps runs in reverse order and the
    original exception is re-raised wrapped in
    :class:`DevEnvProvisioningError`. Steps 4-7 are idempotent; if any
    fail, the operator re-runs ``minds env deploy`` after addressing
    the cause.
    """
    completed_creation_steps: list[str] = []
    neon_record: NeonProjectRecord | None = None
    supertokens_record: SuperTokensAppRecord | None = None

    try:
        with info_span("Ensuring Modal environment {!r}", str(name)):
            providers.ensure_modal_env(name, parent_concurrency_group)
            completed_creation_steps.append("modal_env")

        with info_span("Ensuring Neon project for {!r}", str(name)):
            neon_record = providers.create_neon_project(
                name, credentials.neon_org_id, credentials.neon_api_token, parent_concurrency_group
            )
            completed_creation_steps.append("neon_project")

        with info_span("Ensuring SuperTokens app for {!r}", str(name)):
            supertokens_record = providers.create_supertokens_app(
                name,
                credentials.supertokens_core_url,
                credentials.supertokens_api_key,
            )
            completed_creation_steps.append("supertokens_app")
    except _PROVIDER_ERRORS as exc:
        _best_effort_rollback(
            name=name,
            completed_steps=completed_creation_steps,
            providers=providers,
            credentials=credentials,
            parent_concurrency_group=parent_concurrency_group,
        )
        raise DevEnvProvisioningError(
            f"Failed to provision dev env {name!r}: {exc!s}. "
            f"Rolled back: {completed_creation_steps[::-1] or 'nothing was created yet'}."
        ) from exc

    assert neon_record is not None
    assert supertokens_record is not None

    modal_workspace = str(deploy_config.modal_workspace)
    tier_vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    # For dev-env deploys the Modal env is always the env name itself, so
    # two devs never share one Modal env. Tier deploys (see
    # :func:`deploy_tier_env`) use ``deploy_config.modal_env`` instead.
    modal_env = str(name)

    # First pass: push every per-env Modal Secret using URLs we can know
    # up front (per-env Neon DSN, per-env SuperTokens app URI). Modal
    # Secrets must exist before `modal deploy` will accept the deploy,
    # so this happens first. AUTH_WEBSITE_DOMAIN and the connector URL
    # we'd want in litellm-connector are filled in later, after the
    # first connector deploy gives us the real URL.
    first_pass_overrides = compute_per_env_overrides(
        name,
        modal_workspace=modal_workspace,
        neon_record=neon_record,
        supertokens_record=supertokens_record,
    )
    with info_span("Pushing initial per-env Modal Secrets into env {!r}", modal_env):
        litellm_master_key = _read_litellm_master_key(
            tier_vault_prefix,
            providers,
            parent_concurrency_group,
        )
        for service in per_env_secret_services():
            per_service_overrides = dict(first_pass_overrides.get(service, {}))
            # Auto-populate litellm-connector with the master key from the
            # litellm Vault entry. LITELLM_PROXY_URL is filled in second-pass
            # below once we know the actual proxy URL. Also push the env
            # name so the connector can tag Cloudflare tunnels with their
            # owning minds env (used by destroy to enumerate + delete).
            if service == "litellm-connector":
                if litellm_master_key:
                    per_service_overrides.setdefault("LITELLM_MASTER_KEY", litellm_master_key)
                per_service_overrides.setdefault(MINDS_ENV_NAME_KEY, str(name))
            with info_span("Pushing per-env Modal Secret {!r}", f"{service}-{tier}"):
                values = providers.read_per_env_secret_values(
                    service,
                    tier_vault_prefix,
                    per_service_overrides,
                    parent_concurrency_group,
                )
                providers.push_per_env_modal_secret(
                    f"{service}-{tier}",
                    values,
                    modal_env,
                    parent_concurrency_group,
                )

    litellm_proxy_min_containers = int(deploy_config.min_containers.litellm_proxy)
    connector_min_containers = int(deploy_config.min_containers.connector)

    # Under the shortened app + function names (``rsc-<tier>``/``api``
    # and ``llm-<tier>``/``proxy``) the natural Modal hostname always
    # fits under DNS's 63-char limit for any valid ``DevEnvName``, so
    # the URL we computed up front (via ``per_env_connector_url`` /
    # ``per_env_litellm_proxy_url``) is exactly the URL Modal will
    # assign. That means every URL-dependent secret value
    # (``supertokens.AUTH_WEBSITE_DOMAIN``,
    # ``litellm-connector.LITELLM_PROXY_URL``) is correct on the FIRST
    # secret push -- no second-pass re-push, no connector redeploy. We
    # still assert below that the URLs Modal reported back match the
    # ones we computed, so a future Modal URL-scheme change surfaces
    # immediately instead of silently breaking auth links.
    expected_litellm_proxy_url = per_env_litellm_proxy_url(name, modal_workspace)
    expected_connector_url = per_env_connector_url(name, modal_workspace)

    with info_span(
        "Deploying llm-{} into env {!r} (min_containers={})",
        tier,
        modal_env,
        litellm_proxy_min_containers,
    ):
        litellm_proxy_url = providers.deploy_litellm_proxy(
            modal_env, tier, litellm_proxy_min_containers, parent_concurrency_group
        )
    _assert_deploy_url_matches(actual=litellm_proxy_url, expected=expected_litellm_proxy_url, app=f"llm-{tier}")

    with info_span(
        "Deploying rsc-{} into env {!r} (min_containers={})",
        tier,
        modal_env,
        connector_min_containers,
    ):
        connector_url = providers.deploy_remote_service_connector(
            modal_env, tier, connector_min_containers, parent_concurrency_group
        )
    _assert_deploy_url_matches(actual=connector_url, expected=expected_connector_url, app=f"rsc-{tier}")

    public_config = ClientEnvConfig(
        connector_url=connector_url,
        litellm_proxy_url=litellm_proxy_url,
    )
    client_path = write_client_config(public_config, name=name)
    secrets_path = write_secrets_file(
        {
            # The two DSNs the deployed connector + LiteLLM proxy talk
            # to at runtime. ``NEON_HOST_POOL_DSN`` is also the value
            # that ``mngr imbue_cloud admin pool create`` (when run
            # from this activated shell) reads as the default for its
            # ``--database-url`` argument.
            "NEON_HOST_POOL_DSN": neon_record.host_pool_dsn,
            "NEON_LITELLM_DSN": neon_record.litellm_cost_dsn,
            "SUPERTOKENS_CONNECTION_URI": SecretStr(supertokens_record.connection_uri),
            "SUPERTOKENS_API_KEY": supertokens_record.api_key,
        },
        name=name,
    )

    return DeployedDevEnv(
        name=name,
        client_config_path=str(client_path),
        secrets_path=str(secrets_path),
        connector_url=connector_url,
        litellm_proxy_url=litellm_proxy_url,
    )


_DEV_TIER: Final[str] = "dev"


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
      ``activate``-time auto-wipe across developers; dev envs and
      production don't (see :func:`tier_uses_generation_tracking`).

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

    Raises :class:`DevEnvNotFoundError` if no env root exists -- the
    operator is asked to confirm the name they meant.
    """
    if not env_root_exists(name):
        raise DevEnvNotFoundError(f"No env root for env {name!r} at {env_root_dir(name)}; nothing to destroy.")

    tier_vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    is_dev_tier = tier == _DEV_TIER
    # For dev-tier deploys the Modal env is the env name (each dev gets
    # their own); for shared tiers it's the tier's stable Modal env
    # from deploy.toml (``main`` by convention).
    modal_env_for_tier_ops = str(name) if is_dev_tier else str(deploy_config.modal_env)

    # Step 1: mngr agents first, so their docker containers / pool
    # hosts / tunnels stop cleanly before we tear down the cloud
    # resources they reference.
    if keep_agents:
        logger.warning(
            "minds env destroy {!r}: --keep-agents passed; skipping mngr-agent teardown. "
            "Run `mngr destroy <agent>` manually for any agents bound to this env.",
            str(name),
        )
    else:
        with info_span("Destroying mngr agents under env {!r}", str(name)):
            destroyed_count = destroy_all_mngr_agents_in_env(
                name,
                destroy_agent=providers.destroy_mngr_agent,
                parent_concurrency_group=parent_concurrency_group,
            )
            if destroyed_count:
                logger.info("Destroyed {} mngr agent(s) under env {!r}", destroyed_count, str(name))

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
    if is_dev_tier:
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
    if is_dev_tier:
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
    if is_dev_tier:
        with info_span("Deleting Modal environment {!r} (cascade-deletes apps + secrets)", str(name)):
            providers.delete_modal_env(name, parent_concurrency_group)
    else:
        with info_span("Stopping Modal apps for env {!r} in Modal env {!r}", str(name), modal_env_for_tier_ops):
            for app_name in (f"llm-{tier}", f"rsc-{tier}"):
                providers.stop_modal_app(app_name, modal_env_for_tier_ops, parent_concurrency_group)
        with info_span("Deleting per-tier Modal Secrets in env {!r}", modal_env_for_tier_ops):
            for service in deploy_config.secrets.services:
                providers.delete_modal_secret(f"{service}-{tier}", modal_env_for_tier_ops, parent_concurrency_group)

    # Step 7: generation id removal -- ONLY for tiers that use generation
    # tracking (i.e. shared tiers like staging). For dev / production,
    # there is no generation Vault entry to remove. This is the one
    # genuinely tier-specific step (everything else above is the same
    # flow with tier-driven cleanup ops).
    if tier_uses_generation_tracking(tier):
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


def deploy_tier_env(
    *,
    tier: str,
    deploy_config: DeployEnvConfig,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
) -> DeployedTierEnv:
    """Push tier-shared secrets and deploy the Modal apps for ``tier``.

    For ``staging`` / ``production`` tiers. Reads every service named in
    ``deploy_config.secrets.services`` straight from Vault (no per-env
    overrides), pushes them into Modal as ``<service>-<tier>`` Secrets,
    and runs ``modal deploy`` for both ``llm-<tier>`` and ``rsc-<tier>``
    into ``deploy_config.modal_env``.

    Writes nothing to disk: the URLs are deterministic from the tier's
    Modal workspace + app names, and the committed in-repo
    ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` is the
    source of truth for what ``minds run`` should talk to.

    Idempotent: re-runs upsert the same Modal Secrets and overwrite the
    Modal app deploys in place.
    """
    tier_vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    modal_env = str(deploy_config.modal_env)
    services = tuple(str(s) for s in deploy_config.secrets.services)

    # Mint (or look up) the tier generation id BEFORE pushing secrets
    # -- the id rides along in the litellm-connector secret so the
    # deployed connector can expose it via ``/generation``. Only minted
    # for shared tiers (staging today; not dev / production -- see
    # :func:`tier_uses_generation_tracking` for the rationale).
    generation_id: str | None = None
    if tier_uses_generation_tracking(tier):
        generation_id = providers.ensure_generation_id(tier_vault_prefix, parent_concurrency_group)
        logger.info("Tier {!r} generation id: {}", tier, generation_id)

    logger.info(
        "Pushing tier-shared Modal Secrets for tier {!r} into Modal env {!r}...",
        tier,
        modal_env,
    )
    for service in services:
        # Tier deploys have no per-env overrides except the generation
        # id and the env name, both of which the connector reads at
        # startup -- the id powers ``/generation``, the env name is the
        # Cloudflare-tunnel metadata tag. Both ride in the
        # ``litellm-connector-<tier>`` secret since that's where the
        # connector's other custom env values already live.
        overrides: dict[str, str] = {}
        if service == "litellm-connector":
            if generation_id is not None:
                overrides[GENERATION_ID_KEY] = generation_id
            overrides[MINDS_ENV_NAME_KEY] = tier
        values = providers.read_per_env_secret_values(
            service,
            tier_vault_prefix,
            overrides,
            parent_concurrency_group,
        )
        providers.push_per_env_modal_secret(
            f"{service}-{tier}",
            values,
            modal_env,
            parent_concurrency_group,
        )

    litellm_proxy_min_containers = int(deploy_config.min_containers.litellm_proxy)
    connector_min_containers = int(deploy_config.min_containers.connector)

    # Computed expected URLs (deterministic under the shortened app +
    # function names); we assert against the Modal-reported URLs below
    # so a future Modal URL-scheme change surfaces immediately.
    modal_workspace = str(deploy_config.modal_workspace)
    expected_litellm_proxy_url = tier_litellm_proxy_url(tier, modal_workspace)
    expected_connector_url = tier_connector_url(tier, modal_workspace)

    logger.info(
        "Deploying llm-{} into Modal env {!r} (min_containers={})...",
        tier,
        modal_env,
        litellm_proxy_min_containers,
    )
    litellm_proxy_url = providers.deploy_litellm_proxy(
        modal_env, tier, litellm_proxy_min_containers, parent_concurrency_group
    )
    _assert_deploy_url_matches(actual=litellm_proxy_url, expected=expected_litellm_proxy_url, app=f"llm-{tier}")

    logger.info(
        "Deploying rsc-{} into Modal env {!r} (min_containers={})...",
        tier,
        modal_env,
        connector_min_containers,
    )
    connector_url = providers.deploy_remote_service_connector(
        modal_env, tier, connector_min_containers, parent_concurrency_group
    )
    _assert_deploy_url_matches(actual=connector_url, expected=expected_connector_url, app=f"rsc-{tier}")

    return DeployedTierEnv(
        tier=tier,
        modal_env=modal_env,
        connector_url=connector_url,
        litellm_proxy_url=litellm_proxy_url,
    )


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


def _best_effort_rollback(
    *,
    name: DevEnvName,
    completed_steps: list[str],
    providers: Providers,
    credentials: ProviderCredentials,
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    """Walk completed creation steps in reverse, swallowing per-step failures."""
    for step in reversed(completed_steps):
        rollback_fn = _ROLLBACK_TABLE.get(step)
        if rollback_fn is None:
            logger.warning("Unknown rollback step {!r} for dev env {!r}; skipping", step, str(name))
            continue
        try:
            rollback_fn(name, providers, credentials, parent_concurrency_group)
        except _PROVIDER_ERRORS as exc:
            logger.warning("Rollback of {!r} step for dev env {!r} failed: {}", step, str(name), exc)


def _rollback_modal_env(
    name: DevEnvName,
    providers: "Providers",
    credentials: "ProviderCredentials",
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    providers.delete_modal_env(name, parent_concurrency_group)


def _rollback_neon_project(
    name: DevEnvName,
    providers: "Providers",
    credentials: "ProviderCredentials",
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    providers.delete_neon_project(name, credentials.neon_org_id, credentials.neon_api_token)


def _rollback_supertokens_app(
    name: DevEnvName,
    providers: "Providers",
    credentials: "ProviderCredentials",
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    providers.delete_supertokens_app(
        name,
        credentials.supertokens_core_url,
        credentials.supertokens_api_key,
    )


_ROLLBACK_TABLE: dict[
    str,
    Callable[[DevEnvName, "Providers", "ProviderCredentials", ConcurrencyGroup], None],
] = {
    "modal_env": _rollback_modal_env,
    "neon_project": _rollback_neon_project,
    "supertokens_app": _rollback_supertokens_app,
}


def list_dev_envs() -> tuple[DevEnvSummary, ...]:
    """Return one :class:`DevEnvSummary` per ``~/.minds*/`` directory on disk.

    Globs the user's home for every env root, including ``~/.minds/``
    (production) and ``~/.minds-staging/`` if they exist. Each row
    carries the env name, the absolute env-root path, and -- if the
    per-env ``client.toml`` is present and parseable -- the
    ``connector_url`` parsed out of it. Rows for env roots that have
    no ``client.toml`` (e.g. ``staging`` whose URLs live in the
    in-repo file, not under the env root) leave ``connector_url`` and
    ``client_config_path`` as ``None`` -- the CLI renders those as
    "no client.toml under env_root".
    """
    summaries: list[DevEnvSummary] = []
    for env_root in list_env_root_dirs():
        env_name = _env_name_from_root_path(env_root)
        client_path: Path | None = None
        connector_url: AnyUrl | None = None
        if env_name != "production":
            dev_env_name = DevEnvName(env_name)
            if client_config_exists(dev_env_name):
                client_path = client_config_file(dev_env_name)
                connector_url = read_client_config_file(dev_env_name).connector_url
        summaries.append(
            DevEnvSummary(
                name=env_name,
                env_root=str(env_root),
                client_config_path=str(client_path) if client_path is not None else None,
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
    "DeployedDevEnv",
    "DeployedTierEnv",
    "DevEnvSummary",
    "ProviderCredentials",
    "Providers",
    "build_per_env_secret_values",
    "delete_modal_secret",
    "deploy_dev_env",
    "deploy_litellm_proxy",
    "deploy_remote_service_connector",
    "deploy_tier_env",
    "destroy_env",
    "ensure_modal_env",
    "list_dev_envs",
    "push_per_env_modal_secret",
    "stop_modal_app",
]
