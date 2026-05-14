"""Orchestrate ``minds env create / list / destroy`` flows.

The orchestration is split into pure logic (this module) and CLI plumbing
(``imbue.minds.cli.env``). The pure side takes a :class:`Providers` bundle
so tests can swap in fakes for each external dependency; the CLI side
constructs the real providers (Modal CLI, Neon HTTP, SuperTokens HTTP,
Vultr HTTP) at runtime.

Failure model for ``create_dev_env``: best-effort cleanup. If step N fails,
we attempt to delete every resource the previous N-1 steps created (in
reverse order) and then re-raise wrapped in :class:`DevEnvProvisioningError`.
The local TOML file is only written after all provisioning succeeds, so a
failed ``create`` never leaves a stale ``~/.minds/envs/<name>.toml`` behind.
"""

from collections.abc import Callable
from typing import Final

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.envs.local_store import LocalDevEnvConfig
from imbue.minds.envs.local_store import delete_dev_env_file
from imbue.minds.envs.local_store import list_dev_env_files
from imbue.minds.envs.local_store import read_dev_env_file
from imbue.minds.envs.local_store import write_dev_env_file
from imbue.minds.envs.paths import dev_env_file
from imbue.minds.envs.primitives import DevEnvAlreadyExistsError
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import DevEnvProvisioningError
from imbue.minds.envs.providers.neon_db import NeonDatabaseRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.vultr_tags import VultrInstanceSummary

_DEV_TIER_NAME: Final[str] = "dev"


class ProviderCredentials(FrozenModel):
    """Per-provider credentials read from the dev-tier Vault secrets.

    Each dynamic dev env shares these dev-tier creds (the user's whole
    point in flagging that dev secrets stay local-only): minds reads them
    fresh from Vault for the duration of an ``env create`` invocation and
    does not persist them.
    """

    neon_project_id: str = Field(description="Dev-tier Neon project id under which per-dev-env DBs are created.")
    neon_api_token: SecretStr = Field(description="Dev-tier Neon API token.")
    supertokens_core_url: str = Field(description="Dev-tier SuperTokens core base URL.")
    supertokens_api_key: SecretStr = Field(description="Dev-tier SuperTokens admin API key.")
    vultr_api_key: SecretStr = Field(description="Dev-tier Vultr API key (shared across dev envs).")


# Type aliases for the injectable provider callables. Tests substitute
# fakes here; the CLI wires the real provider modules at runtime.
CreateModalEnvFn = Callable[[DevEnvName], None]
DeleteModalEnvFn = Callable[[DevEnvName], None]
CreateNeonDbFn = Callable[[DevEnvName, str, SecretStr], NeonDatabaseRecord]
DeleteNeonDbFn = Callable[[DevEnvName, str, SecretStr], None]
CreateSuperTokensAppFn = Callable[[DevEnvName, str, SecretStr], SuperTokensAppRecord]
DeleteSuperTokensAppFn = Callable[[DevEnvName, str, SecretStr], None]
ListVultrInstancesFn = Callable[[DevEnvName, SecretStr], tuple[VultrInstanceSummary, ...]]
DeleteVultrInstancesFn = Callable[[tuple[VultrInstanceSummary, ...], SecretStr], None]


class Providers(FrozenModel):
    """Injectable provider bundle.

    Defaulting fields would tempt callers to omit them and silently get
    no-op behaviour, so every field is required.
    """

    model_config = {"arbitrary_types_allowed": True}

    create_modal_env: CreateModalEnvFn = Field(description="Create a Modal environment for the dev env.")
    delete_modal_env: DeleteModalEnvFn = Field(description="Delete the Modal environment.")
    create_neon_db: CreateNeonDbFn = Field(description="Create a per-dev-env Neon database.")
    delete_neon_db: DeleteNeonDbFn = Field(description="Delete the per-dev-env Neon database.")
    create_supertokens_app: CreateSuperTokensAppFn = Field(description="Create a per-dev-env SuperTokens app.")
    delete_supertokens_app: DeleteSuperTokensAppFn = Field(description="Delete the per-dev-env SuperTokens app.")
    list_vultr_instances: ListVultrInstancesFn = Field(description="List Vultr instances tagged for this dev env.")
    delete_vultr_instances: DeleteVultrInstancesFn = Field(description="Delete the listed Vultr instances.")


class CreatedDevEnv(FrozenModel):
    """Summary returned by :func:`create_dev_env`."""

    name: DevEnvName
    config_path: str = Field(description="Path to the ~/.minds/envs/<name>.toml that was written.")
    connector_url: AnyUrl
    litellm_proxy_url: AnyUrl


class DevEnvSummary(FrozenModel):
    """One row of :func:`list_dev_envs`."""

    name: DevEnvName
    config_path: str
    connector_url: AnyUrl


def _build_dev_env_urls(name: DevEnvName, deploy: DeployEnvConfig) -> tuple[AnyUrl, AnyUrl]:
    """Derive (connector_url, litellm_proxy_url) for a dynamic dev env.

    Modal exposes asgi apps at ``<workspace>--<app-name>-<function>.modal.run``
    where ``<app-name>`` is the deploy-time name (``remote-service-connector-<env>``
    here). For dynamic dev envs the env *name* equals ``<dev-name>``, so the
    pattern is ``<workspace>--remote-service-connector-<dev-name>-fastapi-app.modal.run``.
    LiteLLM follows the same shape.
    """
    workspace = str(deploy.modal_workspace)
    connector = f"https://{workspace}--remote-service-connector-{name}-fastapi-app.modal.run"
    litellm = f"https://{workspace}--litellm-proxy-{name}-litellm-app.modal.run"
    return AnyUrl(connector), AnyUrl(litellm)


def create_dev_env(
    name: DevEnvName,
    *,
    deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials,
    providers: Providers,
    root_name: str | None = None,
) -> CreatedDevEnv:
    """Provision a new dynamic dev env, rolling back on partial failure.

    Steps in order:

    1. Refuse if ``~/.minds/envs/<name>.toml`` already exists.
    2. Create the Modal environment.
    3. Create the Neon database.
    4. Create the SuperTokens app.
    5. Write the local TOML.

    On any step failing, the cleanup for previously-completed steps runs
    in reverse order and the original exception is re-raised wrapped in
    :class:`DevEnvProvisioningError`. Vultr is intentionally not touched
    at create time -- it is only consulted during destroy to clean up any
    instances the operator later tagged with this dev env.
    """
    target_path = dev_env_file(name, root_name=root_name)
    if target_path.exists():
        raise DevEnvAlreadyExistsError(
            f"Dev env {name!r} already has a local file at {target_path}. Run `minds env destroy {name}` first."
        )

    completed_steps: list[str] = []
    neon_record: NeonDatabaseRecord | None = None
    supertokens_record: SuperTokensAppRecord | None = None

    try:
        logger.info("Creating Modal environment {!r}...", str(name))
        providers.create_modal_env(name)
        completed_steps.append("modal_env")

        logger.info("Creating Neon database for {!r}...", str(name))
        neon_record = providers.create_neon_db(name, credentials.neon_project_id, credentials.neon_api_token)
        completed_steps.append("neon_db")

        logger.info("Creating SuperTokens app for {!r}...", str(name))
        supertokens_record = providers.create_supertokens_app(
            name,
            credentials.supertokens_core_url,
            credentials.supertokens_api_key,
        )
        completed_steps.append("supertokens_app")
    except Exception as exc:
        _best_effort_rollback(
            name=name,
            completed_steps=completed_steps,
            providers=providers,
            credentials=credentials,
        )
        raise DevEnvProvisioningError(
            f"Failed to provision dev env {name!r}: {exc!s}. "
            f"Rolled back: {completed_steps[::-1] or 'nothing was created yet'}."
        ) from exc

    assert neon_record is not None
    assert supertokens_record is not None

    connector_url, litellm_proxy_url = _build_dev_env_urls(name, deploy_config)
    client = ClientEnvConfig(connector_url=connector_url, litellm_proxy_url=litellm_proxy_url)
    local_config = LocalDevEnvConfig(
        name=name,
        client=client,
        secrets={
            "NEON_POOLED_DSN": neon_record.pooled_dsn,
            "SUPERTOKENS_CONNECTION_URI": SecretStr(supertokens_record.connection_uri),
            "SUPERTOKENS_API_KEY": supertokens_record.api_key,
        },
    )
    config_path = write_dev_env_file(local_config, root_name=root_name)

    return CreatedDevEnv(
        name=name,
        config_path=str(config_path),
        connector_url=connector_url,
        litellm_proxy_url=litellm_proxy_url,
    )


def _best_effort_rollback(
    *,
    name: DevEnvName,
    completed_steps: list[str],
    providers: Providers,
    credentials: ProviderCredentials,
) -> None:
    """Walk completed steps in reverse, swallowing per-step failures.

    A second failure during rollback is logged but does not mask the
    original ``create`` failure -- the user sees the original error in the
    raised :class:`DevEnvProvisioningError` and is told which steps got
    rolled back.
    """
    for step in reversed(completed_steps):
        try:
            if step == "supertokens_app":
                providers.delete_supertokens_app(
                    name,
                    credentials.supertokens_core_url,
                    credentials.supertokens_api_key,
                )
            elif step == "neon_db":
                providers.delete_neon_db(
                    name,
                    credentials.neon_project_id,
                    credentials.neon_api_token,
                )
            elif step == "modal_env":
                providers.delete_modal_env(name)
        except Exception as exc:
            logger.warning("Rollback of {!r} step for dev env {!r} failed: {}", step, str(name), exc)


def destroy_dev_env(
    name: DevEnvName,
    *,
    credentials: ProviderCredentials,
    providers: Providers,
    keep_agents: bool = False,
    root_name: str | None = None,
) -> None:
    """Tear down every resource ``create_dev_env`` provisioned.

    Order is the reverse of create: SuperTokens, Neon, Modal, then any
    Vultr instances tagged with this dev env. Finally the local TOML file
    is removed. ``keep_agents`` is reserved for the eventual ``mngr destroy``
    integration; the bare provisioning path does not own running agents.

    Raises :class:`DevEnvNotFoundError` if no local file exists -- the
    operator is asked to confirm the name they meant.
    """
    if not dev_env_file(name, root_name=root_name).is_file():
        raise DevEnvNotFoundError(f"No local file for dev env {name!r}; nothing to destroy.")
    _ = keep_agents  # placeholder for the eventual `mngr destroy` integration

    instances = providers.list_vultr_instances(name, credentials.vultr_api_key)
    if instances:
        providers.delete_vultr_instances(instances, credentials.vultr_api_key)

    providers.delete_supertokens_app(
        name,
        credentials.supertokens_core_url,
        credentials.supertokens_api_key,
    )
    providers.delete_neon_db(name, credentials.neon_project_id, credentials.neon_api_token)
    providers.delete_modal_env(name)

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
                connector_url=config.client.connector_url,
            )
        )
    return tuple(summaries)
