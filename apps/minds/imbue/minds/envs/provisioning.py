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
real Modal CLI / Neon HTTP / SuperTokens HTTP / Vultr HTTP / Modal
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

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.envs.local_store import client_config_exists
from imbue.minds.envs.local_store import delete_env_root
from imbue.minds.envs.local_store import env_root_exists
from imbue.minds.envs.local_store import read_client_config_file
from imbue.minds.envs.local_store import write_client_config
from imbue.minds.envs.local_store import write_secrets_file
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.paths import list_env_root_dirs
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
# (modal_env, tier, cg) -> deployed URL. ``modal_env`` is the dev env
# name for dev-tier deploys or the tier's stable Modal env (``main`` by
# convention) for staging / production deploys.
DeployModalAppFn = Callable[[str, str, ConcurrencyGroup], AnyUrl]
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
        description="(secret_name, values, modal_env, cg) -> upsert the Modal Secret in the named Modal env.",
    )
    deploy_litellm_proxy: DeployModalAppFn = Field(
        description="(modal_env, tier, cg) -> `modal deploy` the litellm-proxy app into ``modal_env``.",
    )
    deploy_remote_service_connector: DeployModalAppFn = Field(
        description="(modal_env, tier, cg) -> `modal deploy` the connector app into ``modal_env``.",
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

    name: str = Field(description="The env name (e.g. 'josh-3'), or 'production' for ~/.minds/.")
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
    5. Deploy ``litellm-proxy-<tier>`` into Modal env ``<name>``.
    6. Deploy ``remote-service-connector-<tier>`` into Modal env ``<name>``.
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
    logger.info("Pushing initial per-env Modal Secrets into env {!r}...", modal_env)
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
            modal_env,
            parent_concurrency_group,
        )

    logger.info("Deploying litellm-proxy-{} into env {!r}...", tier, modal_env)
    litellm_proxy_url = providers.deploy_litellm_proxy(modal_env, tier, parent_concurrency_group)

    logger.info("Deploying remote-service-connector-{} into env {!r}...", tier, modal_env)
    connector_url = providers.deploy_remote_service_connector(modal_env, tier, parent_concurrency_group)

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
        modal_env,
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
        modal_env,
        parent_concurrency_group,
    )

    logger.info("Redeploying remote-service-connector-{} to pick up final secrets...", tier)
    connector_url = providers.deploy_remote_service_connector(modal_env, tier, parent_concurrency_group)

    public_config = ClientEnvConfig(
        connector_url=connector_url,
        litellm_proxy_url=litellm_proxy_url,
    )
    client_path = write_client_config(public_config, name=name)
    secrets_path = write_secrets_file(
        {
            "NEON_POOLED_DSN": neon_record.pooled_dsn,
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
    and runs ``modal deploy`` for both ``litellm-proxy-<tier>`` and
    ``remote-service-connector-<tier>`` into ``deploy_config.modal_env``.

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

    logger.info(
        "Pushing tier-shared Modal Secrets for tier {!r} into Modal env {!r}...",
        tier,
        modal_env,
    )
    for service in services:
        # Tier deploys have no per-env overrides: the value the connector /
        # proxy sees is exactly what's in Vault. ``read_per_env_secret_values``
        # accepts an empty overrides dict for this case.
        values = providers.read_per_env_secret_values(
            service,
            tier_vault_prefix,
            {},
            parent_concurrency_group,
        )
        providers.push_per_env_modal_secret(
            f"{service}-{tier}",
            values,
            modal_env,
            parent_concurrency_group,
        )

    logger.info("Deploying litellm-proxy-{} into Modal env {!r}...", tier, modal_env)
    litellm_proxy_url = providers.deploy_litellm_proxy(modal_env, tier, parent_concurrency_group)

    logger.info("Deploying remote-service-connector-{} into Modal env {!r}...", tier, modal_env)
    connector_url = providers.deploy_remote_service_connector(modal_env, tier, parent_concurrency_group)

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
) -> None:
    """Tear down every resource ``deploy_dev_env`` provisioned and remove the env root.

    Order is the reverse of deploy: any Vultr instances tagged with this
    dev env, SuperTokens app, Neon DB, Modal env. Finally
    ``~/.minds-<name>/`` is removed entirely so subsequent commands fail
    fast on the dangling activation instead of silently re-creating
    partial state under a half-torn-down env root.

    ``keep_agents`` is a forward-compatible knob for the eventual
    ``mngr destroy`` integration. Today the provisioning path does not
    own running workspace agents, so the flag is effectively a no-op:
    running agents are left alone whether it is passed or not. When
    ``keep_agents=False`` (the value implying "tear down everything"),
    a warning is logged so the operator knows manual ``mngr destroy``
    is still required for any agents bound to this env.

    Raises :class:`DevEnvNotFoundError` if no env root exists -- the
    operator is asked to confirm the name they meant.
    """
    if not env_root_exists(name):
        raise DevEnvNotFoundError(f"No env root for dev env {name!r} at {env_root_dir(name)}; nothing to destroy.")
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

    delete_env_root(name)


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
    "deploy_dev_env",
    "deploy_litellm_proxy",
    "deploy_remote_service_connector",
    "deploy_tier_env",
    "destroy_dev_env",
    "ensure_modal_env",
    "list_dev_envs",
    "push_per_env_modal_secret",
]
