"""Orchestrate ``minds env deploy / list / destroy`` flows.

The orchestration is split into pure logic (this module) and CLI plumbing
(``imbue.minds.cli.env``). The pure side takes a :class:`Providers` bundle
so tests can swap in fakes for each external dependency; the CLI side
constructs the real providers (Modal CLI, Neon HTTP, SuperTokens HTTP,
Vultr HTTP, Modal deploy) at runtime.

``deploy_dev_env`` is idempotent: re-running it for an existing dev env
re-pushes Modal Secrets and re-deploys both Modal apps, picking up any
new tier-shared values that landed in Vault since the last run. The
local TOML file is overwritten in place; there is no "already exists"
gate.

Failure model: if any *provider creation* step (Modal env, Neon DB,
SuperTokens app) fails partway through on a fresh deploy, the helper
rolls back whatever it just created and re-raises. The push-secrets and
Modal-deploy steps are intrinsically idempotent (Modal Secret upserts
with ``--force``, Modal deploys overwrite), so they don't need rollback
-- the operator can just re-run ``minds env deploy <name>``.
"""

from collections.abc import Callable

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.envs.local_store import LocalDevEnvConfig
from imbue.minds.envs.local_store import delete_dev_env_file
from imbue.minds.envs.local_store import list_dev_env_files
from imbue.minds.envs.local_store import read_dev_env_file
from imbue.minds.envs.local_store import write_dev_env_file
from imbue.minds.envs.paths import dev_env_file
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.per_env_deploy import build_per_env_secret_values
from imbue.minds.envs.per_env_deploy import compute_per_env_overrides
from imbue.minds.envs.per_env_deploy import deploy_litellm_proxy
from imbue.minds.envs.per_env_deploy import deploy_remote_service_connector
from imbue.minds.envs.per_env_deploy import ensure_modal_env
from imbue.minds.envs.per_env_deploy import per_env_secret_services
from imbue.minds.envs.per_env_deploy import push_per_env_modal_secret
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import DevEnvProvisioningError
from imbue.minds.envs.providers.modal_env import ModalEnvProviderError
from imbue.minds.envs.providers.neon_db import NeonDatabaseRecord
from imbue.minds.envs.providers.neon_db import NeonProviderError
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensProviderError
from imbue.minds.envs.providers.vultr_tags import VultrInstanceSummary
from imbue.minds.errors import MindError

_PROVIDER_ERRORS: tuple[type[Exception], ...] = (
    ModalEnvProviderError,
    NeonProviderError,
    SuperTokensProviderError,
    ModalDeployError,
    MindError,
)


class ProviderCredentials(FrozenModel):
    """Per-provider credentials read from the dev-tier Vault secrets.

    Each dynamic dev env shares these dev-tier creds (the user's whole
    point in flagging that dev secrets stay local-only): minds reads them
    fresh from Vault for the duration of an ``env deploy`` invocation
    and does not persist them.
    """

    neon_project_id: str = Field(description="Dev-tier Neon project id under which per-dev-env DBs are created.")
    neon_api_token: SecretStr = Field(description="Dev-tier Neon API token.")
    supertokens_core_url: str = Field(description="Dev-tier SuperTokens core base URL.")
    supertokens_api_key: SecretStr = Field(description="Dev-tier SuperTokens admin API key.")
    vultr_api_key: SecretStr = Field(description="Dev-tier Vultr API key (shared across dev envs).")


# Type aliases for the injectable provider callables. Tests substitute
# fakes here; the CLI wires the real provider modules at runtime.
CreateNeonDbFn = Callable[[DevEnvName, str, SecretStr], NeonDatabaseRecord]
DeleteNeonDbFn = Callable[[DevEnvName, str, SecretStr], None]
CreateSuperTokensAppFn = Callable[[DevEnvName, str, SecretStr], SuperTokensAppRecord]
DeleteSuperTokensAppFn = Callable[[DevEnvName, str, SecretStr], None]
ListVultrInstancesFn = Callable[[DevEnvName, SecretStr], tuple[VultrInstanceSummary, ...]]
DeleteVultrInstancesFn = Callable[[tuple[VultrInstanceSummary, ...], SecretStr], None]
ModalEnvOpFn = Callable[[DevEnvName, ConcurrencyGroup], None]
PushPerEnvSecretFn = Callable[[str, dict[str, str], str, ConcurrencyGroup], None]
DeployModalAppFn = Callable[[DevEnvName, str, ConcurrencyGroup], AnyUrl]
ReadPerEnvSecretValuesFn = Callable[[str, str, dict[str, str], ConcurrencyGroup], dict[str, str]]


class Providers(FrozenModel):
    """Injectable provider bundle.

    All fields are required so tests can't silently get default no-op
    behaviour by forgetting one.
    """

    model_config = {"arbitrary_types_allowed": True}

    ensure_modal_env: ModalEnvOpFn = Field(description="Create the Modal environment (idempotent).")
    delete_modal_env: ModalEnvOpFn = Field(description="Delete the Modal environment.")
    create_neon_db: CreateNeonDbFn = Field(description="Create or look up the per-dev-env Neon database.")
    delete_neon_db: DeleteNeonDbFn = Field(description="Delete the per-dev-env Neon database.")
    create_supertokens_app: CreateSuperTokensAppFn = Field(description="Create the per-dev-env SuperTokens app.")
    delete_supertokens_app: DeleteSuperTokensAppFn = Field(description="Delete the per-dev-env SuperTokens app.")
    list_vultr_instances: ListVultrInstancesFn = Field(description="List Vultr instances tagged for this dev env.")
    delete_vultr_instances: DeleteVultrInstancesFn = Field(description="Delete the listed Vultr instances.")
    read_per_env_secret_values: ReadPerEnvSecretValuesFn = Field(
        description="(service, tier_vault_prefix, overrides, cg) -> merged values dict for one Modal Secret.",
    )
    push_per_env_modal_secret: PushPerEnvSecretFn = Field(
        description="(secret_name, values, modal_env, cg) -> upsert the Modal Secret in the per-env Modal env.",
    )
    deploy_litellm_proxy: DeployModalAppFn = Field(
        description="(name, tier, cg) -> `modal deploy` the litellm-proxy app into env <name>.",
    )
    deploy_remote_service_connector: DeployModalAppFn = Field(
        description="(name, tier, cg) -> `modal deploy` the connector app into env <name>.",
    )


class DeployedDevEnv(FrozenModel):
    """Summary returned by :func:`deploy_dev_env`."""

    name: DevEnvName
    config_path: str = Field(description="Path to the ~/.minds/envs/<name>.toml that was written.")
    connector_url: AnyUrl
    litellm_proxy_url: AnyUrl


class DevEnvSummary(FrozenModel):
    """One row of :func:`list_dev_envs`."""

    name: DevEnvName
    config_path: str
    connector_url: AnyUrl


def deploy_dev_env(
    name: DevEnvName,
    *,
    tier: str,
    deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
    root_name: str | None = None,
) -> DeployedDevEnv:
    """Provision (or upgrade) the dev env named ``name``.

    Steps, in order:

    1. Ensure the Modal env exists (idempotent).
    2. Create or look up the per-dev-env Neon database.
    3. Create or look up the per-dev-env SuperTokens app.
    4. Push every per-env Modal Secret (tier-shared Vault values + per-env
       overrides; placeholder for Vault entries that aren't populated).
    5. Deploy ``litellm-proxy-<tier>`` into env ``<name>``.
    6. Deploy ``remote-service-connector-<tier>`` into env ``<name>``.
    7. Write ``~/.<root>/envs/<name>.toml`` (overwriting any existing).

    On any *provider creation* step (1-3) failing, the cleanup for
    previously-completed creation steps runs in reverse order and the
    original exception is re-raised wrapped in
    :class:`DevEnvProvisioningError`. Steps 4-7 are idempotent; if any
    fail, the operator re-runs ``minds env deploy <name>`` after
    addressing the cause.
    """
    completed_creation_steps: list[str] = []
    neon_record: NeonDatabaseRecord | None = None
    supertokens_record: SuperTokensAppRecord | None = None

    try:
        logger.info("Ensuring Modal environment {!r}...", str(name))
        providers.ensure_modal_env(name, parent_concurrency_group)
        completed_creation_steps.append("modal_env")

        logger.info("Ensuring Neon database for {!r}...", str(name))
        neon_record = providers.create_neon_db(name, credentials.neon_project_id, credentials.neon_api_token)
        completed_creation_steps.append("neon_db")

        logger.info("Ensuring SuperTokens app for {!r}...", str(name))
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
    logger.info("Pushing initial per-env Modal Secrets into env {!r}...", str(name))
    litellm_master_key = _read_litellm_master_key(
        tier_vault_prefix,
        providers,
        parent_concurrency_group,
    )
    for service in per_env_secret_services():
        per_service_overrides = dict(first_pass_overrides.get(service, {}))
        # Auto-populate litellm-connector with the master key from the
        # litellm Vault entry. LITELLM_PROXY_URL is filled in second-pass
        # below once we know the actual proxy URL.
        if service == "litellm-connector" and litellm_master_key:
            per_service_overrides.setdefault("LITELLM_MASTER_KEY", litellm_master_key)
        values = providers.read_per_env_secret_values(
            service,
            tier_vault_prefix,
            per_service_overrides,
            parent_concurrency_group,
        )
        providers.push_per_env_modal_secret(
            f"{service}-{tier}",
            values,
            str(name),
            parent_concurrency_group,
        )

    logger.info("Deploying litellm-proxy-{} into env {!r}...", tier, str(name))
    litellm_proxy_url = providers.deploy_litellm_proxy(name, tier, parent_concurrency_group)

    logger.info("Deploying remote-service-connector-{} into env {!r}...", tier, str(name))
    connector_url = providers.deploy_remote_service_connector(name, tier, parent_concurrency_group)

    # Second pass: now that we have the real connector + proxy URLs,
    # update the two Modal Secrets whose values depended on them
    # (supertokens.AUTH_WEBSITE_DOMAIN and litellm-connector.LITELLM_PROXY_URL)
    # and redeploy the connector so the running container picks them up.
    # litellm-proxy doesn't depend on either URL so no redeploy needed.
    logger.info(
        "Re-pushing URL-dependent Modal Secrets with actual deploy URLs (connector={}, litellm={})...",
        connector_url,
        litellm_proxy_url,
    )
    supertokens_values = providers.read_per_env_secret_values(
        "supertokens",
        tier_vault_prefix,
        {
            **first_pass_overrides.get("supertokens", {}),
            "AUTH_WEBSITE_DOMAIN": str(connector_url),
        },
        parent_concurrency_group,
    )
    providers.push_per_env_modal_secret(
        f"supertokens-{tier}",
        supertokens_values,
        str(name),
        parent_concurrency_group,
    )

    litellm_connector_overrides: dict[str, str] = {
        "LITELLM_PROXY_URL": str(litellm_proxy_url),
    }
    if litellm_master_key:
        litellm_connector_overrides["LITELLM_MASTER_KEY"] = litellm_master_key
    litellm_connector_values = providers.read_per_env_secret_values(
        "litellm-connector",
        tier_vault_prefix,
        litellm_connector_overrides,
        parent_concurrency_group,
    )
    providers.push_per_env_modal_secret(
        f"litellm-connector-{tier}",
        litellm_connector_values,
        str(name),
        parent_concurrency_group,
    )

    logger.info("Redeploying remote-service-connector-{} to pick up final secrets...", tier)
    connector_url = providers.deploy_remote_service_connector(name, tier, parent_concurrency_group)

    local_config = LocalDevEnvConfig(
        connector_url=connector_url,
        litellm_proxy_url=litellm_proxy_url,
        secrets={
            "NEON_POOLED_DSN": neon_record.pooled_dsn,
            "SUPERTOKENS_CONNECTION_URI": SecretStr(supertokens_record.connection_uri),
            "SUPERTOKENS_API_KEY": supertokens_record.api_key,
        },
    )
    config_path = write_dev_env_file(local_config, name=name, root_name=root_name, overwrite=True)

    return DeployedDevEnv(
        name=name,
        config_path=str(config_path),
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


def _rollback_neon_db(
    name: DevEnvName,
    providers: "Providers",
    credentials: "ProviderCredentials",
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    providers.delete_neon_db(name, credentials.neon_project_id, credentials.neon_api_token)


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
    "neon_db": _rollback_neon_db,
    "supertokens_app": _rollback_supertokens_app,
}


def destroy_dev_env(
    name: DevEnvName,
    *,
    credentials: ProviderCredentials,
    providers: Providers,
    parent_concurrency_group: ConcurrencyGroup,
    keep_agents: bool = False,
    root_name: str | None = None,
) -> None:
    """Tear down every resource ``deploy_dev_env`` provisioned.

    Order is the reverse of deploy: SuperTokens, Neon, Modal, then any
    Vultr instances tagged with this dev env. Finally the local TOML file
    is removed.

    ``keep_agents`` is a forward-compatible knob for the eventual
    ``mngr destroy`` integration. Today the provisioning path does not
    own running workspace agents, so the flag is effectively a no-op:
    running agents are left alone whether it is passed or not. When
    ``keep_agents=False`` (the value implying "tear down everything"),
    a warning is logged so the operator knows manual ``mngr destroy``
    is still required for any agents bound to this env.

    Raises :class:`DevEnvNotFoundError` if no local file exists -- the
    operator is asked to confirm the name they meant.
    """
    if not dev_env_file(name, root_name=root_name).is_file():
        raise DevEnvNotFoundError(f"No local file for dev env {name!r}; nothing to destroy.")
    if not keep_agents:
        logger.warning(
            "minds env destroy {!r}: workspace-agent teardown is not yet implemented. "
            "Run `mngr destroy <agent>` manually for any agents bound to this env.",
            str(name),
        )

    instances = providers.list_vultr_instances(name, credentials.vultr_api_key)
    if instances:
        providers.delete_vultr_instances(instances, credentials.vultr_api_key)

    providers.delete_supertokens_app(
        name,
        credentials.supertokens_core_url,
        credentials.supertokens_api_key,
    )
    providers.delete_neon_db(name, credentials.neon_project_id, credentials.neon_api_token)
    providers.delete_modal_env(name, parent_concurrency_group)

    delete_dev_env_file(name, root_name=root_name)


def list_dev_envs(*, root_name: str | None = None) -> tuple[DevEnvSummary, ...]:
    """Return one :class:`DevEnvSummary` per file under ``~/.<root>/envs/``."""
    files = list_dev_env_files(root_name=root_name)
    summaries: list[DevEnvSummary] = []
    for path in files:
        name = DevEnvName(path.stem)
        config = read_dev_env_file(name, root_name=root_name)
        summaries.append(
            DevEnvSummary(
                name=name,
                config_path=str(path),
                connector_url=config.connector_url,
            )
        )
    return tuple(summaries)


# Re-export the per_env_deploy helpers so the CLI can build a Providers
# bundle without importing both modules.
__all__ = [
    "DeployedDevEnv",
    "DevEnvSummary",
    "ProviderCredentials",
    "Providers",
    "build_per_env_secret_values",
    "deploy_dev_env",
    "deploy_litellm_proxy",
    "deploy_remote_service_connector",
    "destroy_dev_env",
    "ensure_modal_env",
    "list_dev_envs",
    "push_per_env_modal_secret",
]
