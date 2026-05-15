"""``minds env {create,list,destroy} <name>``.

The CLI side constructs real provider callables (Modal CLI / Neon /
SuperTokens / Vultr HTTP) and threads them into the pure orchestration in
:mod:`imbue.minds.envs.provisioning`.

Dev-tier credentials needed for provisioning come from HCP Vault at command
time -- minds never persists them. The user must already be logged in to
the ``vault`` CLI.
"""

import json
from typing import Final

import click
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.primitives import DevEnvAlreadyExistsError
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import DevEnvProvisioningError
from imbue.minds.envs.primitives import InvalidDevEnvNameError
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.providers.modal_env import create_modal_env
from imbue.minds.envs.providers.modal_env import delete_modal_env
from imbue.minds.envs.providers.neon_db import NeonDatabaseRecord
from imbue.minds.envs.providers.neon_db import create_neon_database
from imbue.minds.envs.providers.neon_db import delete_neon_database
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.supertokens_app import create_supertokens_app
from imbue.minds.envs.providers.supertokens_app import delete_supertokens_app
from imbue.minds.envs.providers.vultr_tags import VultrInstanceSummary
from imbue.minds.envs.providers.vultr_tags import delete_instances as delete_vultr_instances
from imbue.minds.envs.providers.vultr_tags import list_dev_env_instances as list_vultr_instances
from imbue.minds.envs.provisioning import CreatedDevEnv
from imbue.minds.envs.provisioning import ProviderCredentials
from imbue.minds.envs.provisioning import Providers
from imbue.minds.envs.provisioning import create_dev_env
from imbue.minds.envs.provisioning import destroy_dev_env
from imbue.minds.envs.provisioning import list_dev_envs
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.errors import MindError
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import write_stdout_line

_DEV_TIER: Final[str] = "dev"


def _create_neon_for_provider(name: DevEnvName, project_id: str, api_token: SecretStr) -> NeonDatabaseRecord:
    return create_neon_database(name, project_id=project_id, api_token=api_token)


def _delete_neon_for_provider(name: DevEnvName, project_id: str, api_token: SecretStr) -> None:
    delete_neon_database(name, project_id=project_id, api_token=api_token)


def _create_supertokens_for_provider(name: DevEnvName, core_base_url: str, api_key: SecretStr) -> SuperTokensAppRecord:
    return create_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)


def _delete_supertokens_for_provider(name: DevEnvName, core_base_url: str, api_key: SecretStr) -> None:
    delete_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)


def _list_vultr_for_provider(name: DevEnvName, api_key: SecretStr) -> tuple[VultrInstanceSummary, ...]:
    return list_vultr_instances(name, api_key=api_key)


def _delete_vultr_for_provider(instances: tuple[VultrInstanceSummary, ...], api_key: SecretStr) -> None:
    delete_vultr_instances(instances, api_key=api_key)


class _CgBoundCreateModalEnv(FrozenModel):
    """Adapter that closes the Modal-env create call over a :class:`ConcurrencyGroup`.

    The :class:`Providers` callable type for create_modal_env is
    ``Callable[[DevEnvName], None]`` -- which our provider module's
    keyword-only ``parent_concurrency_group`` argument is not directly
    compatible with. This adapter exists so the wiring stays explicit
    without nested defs at the wiring site.
    """

    model_config = {"arbitrary_types_allowed": True}

    cg: ConcurrencyGroup = Field(description="Parent group the modal CLI invocation is tied to.")

    def __call__(self, name: DevEnvName) -> None:
        create_modal_env(name, parent_concurrency_group=self.cg)


class _CgBoundDeleteModalEnv(FrozenModel):
    """Adapter that closes the Modal-env delete call over a :class:`ConcurrencyGroup`.

    Mirrors :class:`_CgBoundCreateModalEnv` for the destroy path.
    """

    model_config = {"arbitrary_types_allowed": True}

    cg: ConcurrencyGroup = Field(description="Parent group the modal CLI invocation is tied to.")

    def __call__(self, name: DevEnvName) -> None:
        delete_modal_env(name, parent_concurrency_group=self.cg)


def _build_real_providers(cg: ConcurrencyGroup) -> Providers:
    """Wire the provider modules together with the typed callable signatures.

    Modal-env operations need a :class:`ConcurrencyGroup` to track the
    spawned ``modal`` CLI; the other providers go through ``httpx`` and
    have no subprocess to manage.
    """
    return Providers(
        create_modal_env=_CgBoundCreateModalEnv(cg=cg),
        delete_modal_env=_CgBoundDeleteModalEnv(cg=cg),
        create_neon_db=_create_neon_for_provider,
        delete_neon_db=_delete_neon_for_provider,
        create_supertokens_app=_create_supertokens_for_provider,
        delete_supertokens_app=_delete_supertokens_for_provider,
        list_vultr_instances=_list_vultr_for_provider,
        delete_vultr_instances=_delete_vultr_for_provider,
    )


def _load_dev_credentials_from_vault(vault_prefix: str, *, cg: ConcurrencyGroup) -> ProviderCredentials:
    """Read every per-provider dev-tier credential `minds env` needs from Vault.

    These credentials live in dedicated "admin" Vault entries that are
    intentionally separate from the Modal-pushed entries -- the connector's
    runtime never needs API tokens for creating Neon DBs or VPS instances,
    so co-mingling those tokens with the Modal-pushed Vault paths would
    leak them into the connector's runtime env unnecessarily.

    Paths read here (none are pushed to Modal):

    - ``<vault_prefix>/neon-admin`` -- ``NEON_API_TOKEN``, ``NEON_PROJECT_ID``
    - ``<vault_prefix>/supertokens`` -- ``SUPERTOKENS_CONNECTION_URI``,
      ``SUPERTOKENS_API_KEY`` (read from the Modal-pushed entry; safe to
      read here because the connector also legitimately needs both keys)
    - ``<vault_prefix>/vultr`` -- ``VULTR_API_KEY``
    """
    neon_admin = read_vault_kv(VaultPath(f"{vault_prefix}/neon-admin"), parent_concurrency_group=cg)
    supertokens = read_vault_kv(VaultPath(f"{vault_prefix}/supertokens"), parent_concurrency_group=cg)
    vultr_secret = read_vault_kv(VaultPath(f"{vault_prefix}/vultr"), parent_concurrency_group=cg)

    project_id = neon_admin.get("NEON_PROJECT_ID", "")
    api_token = neon_admin.get("NEON_API_TOKEN", "")
    if not project_id or not api_token:
        raise VaultReadError(
            f"Vault entry {vault_prefix}/neon-admin missing NEON_PROJECT_ID or NEON_API_TOKEN; "
            "see .minds/template/neon-admin.sh for the schema."
        )

    core_url = supertokens.get("SUPERTOKENS_CONNECTION_URI", "")
    core_api_key = supertokens.get("SUPERTOKENS_API_KEY", "")
    if not core_url or not core_api_key:
        raise VaultReadError(
            f"Vault entry {vault_prefix}/supertokens missing SUPERTOKENS_CONNECTION_URI or SUPERTOKENS_API_KEY."
        )

    vultr_api_key = vultr_secret.get("VULTR_API_KEY", "")
    if not vultr_api_key:
        raise VaultReadError(
            f"Vault entry {vault_prefix}/vultr missing VULTR_API_KEY; see .minds/template/vultr.sh for the schema."
        )

    return ProviderCredentials(
        neon_project_id=project_id,
        neon_api_token=SecretStr(api_token),
        supertokens_core_url=core_url,
        supertokens_api_key=SecretStr(core_api_key),
        vultr_api_key=SecretStr(vultr_api_key),
    )


def _emit_json(payload: object, *, output_format: OutputFormat) -> None:
    """Emit a JSON-shaped payload on stdout in the requested format.

    Used by every ``minds env`` subcommand for the non-HUMAN output paths.
    """
    if output_format is OutputFormat.JSON:
        write_stdout_line(json.dumps(payload, indent=2, default=str))
    elif output_format is OutputFormat.JSONL:
        write_stdout_line(json.dumps(payload, default=str))
    else:
        write_stdout_line(str(payload))


def _emit_create_result(result: CreatedDevEnv, *, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.HUMAN:
        logger.info("Created dev env '{}'.", result.name)
        logger.info("  config:    {}", result.config_path)
        logger.info("  connector: {}", result.connector_url)
        logger.info("  litellm:   {}", result.litellm_proxy_url)
        logger.info("Run `minds run --config-file {}` to launch against this env.", result.config_path)
        return
    _emit_json(
        {
            "name": str(result.name),
            "config_path": result.config_path,
            "connector_url": str(result.connector_url),
            "litellm_proxy_url": str(result.litellm_proxy_url),
        },
        output_format=output_format,
    )


def _emit_destroy_result(name: DevEnvName, *, output_format: OutputFormat) -> None:
    """Emit the destroy success summary in the requested format.

    HUMAN mode logs a friendly status line (mirroring the create path);
    JSON / JSONL emit the same structured payload operators have been
    scripting against.
    """
    if output_format is OutputFormat.HUMAN:
        logger.info("Destroyed dev env '{}'.", name)
        return
    _emit_json({"name": str(name), "status": "destroyed"}, output_format=output_format)


@click.group()
def env() -> None:
    """Manage dynamic dev environments."""


@env.command("create")
@click.argument("name", type=str)
@click.pass_context
def env_create(ctx: click.Context, name: str) -> None:
    """Provision a new dynamic dev env named ``NAME``."""
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)
    try:
        dev_env_name = DevEnvName(name)
    except InvalidDevEnvNameError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        deploy_config = load_deploy_config(_DEV_TIER)
    except EnvConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    with ConcurrencyGroup(name="minds-env-create") as cg:
        try:
            credentials = _load_dev_credentials_from_vault(str(deploy_config.vault_path_prefix), cg=cg)
        except VaultReadError as exc:
            raise click.ClickException(str(exc)) from exc

        providers = _build_real_providers(cg)

        try:
            result = create_dev_env(
                dev_env_name,
                deploy_config=deploy_config,
                credentials=credentials,
                providers=providers,
            )
        except (DevEnvAlreadyExistsError, DevEnvProvisioningError) as exc:
            raise click.ClickException(str(exc)) from exc

    _emit_create_result(result, output_format=output_format)


@env.command("list")
@click.pass_context
def env_list(ctx: click.Context) -> None:
    """List the dynamic dev envs configured on this machine."""
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)
    summaries = list_dev_envs()

    if output_format is OutputFormat.HUMAN:
        if not summaries:
            logger.info("No dev envs configured. Run `minds env create <name>` to create one.")
            return
        for s in summaries:
            logger.info("{}\t{}\t{}", s.name, s.connector_url, s.config_path)
        return

    payload = [
        {
            "name": str(s.name),
            "config_path": s.config_path,
            "connector_url": str(s.connector_url),
        }
        for s in summaries
    ]
    if output_format is OutputFormat.JSONL:
        for entry in payload:
            write_stdout_line(json.dumps(entry, default=str))
    else:
        write_stdout_line(json.dumps(payload, indent=2, default=str))


@env.command("destroy")
@click.argument("name", type=str)
@click.option(
    "--keep-agents",
    is_flag=True,
    default=False,
    help=(
        "Forward-compatible flag for the eventual `mngr destroy` integration. "
        "Agent teardown is not yet implemented; running workspace agents are "
        "left alone today regardless of this flag (a warning is logged when "
        "the flag is omitted)."
    ),
)
@click.pass_context
def env_destroy(ctx: click.Context, name: str, keep_agents: bool) -> None:
    """Tear down every resource ``minds env create`` provisioned for ``NAME``."""
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)
    try:
        dev_env_name = DevEnvName(name)
    except InvalidDevEnvNameError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        deploy_config = load_deploy_config(_DEV_TIER)
    except EnvConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    with ConcurrencyGroup(name="minds-env-destroy") as cg:
        try:
            credentials = _load_dev_credentials_from_vault(str(deploy_config.vault_path_prefix), cg=cg)
        except VaultReadError as exc:
            raise click.ClickException(str(exc)) from exc

        providers = _build_real_providers(cg)

        try:
            destroy_dev_env(
                dev_env_name,
                credentials=credentials,
                providers=providers,
                keep_agents=keep_agents,
            )
        except DevEnvNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc
        except MindError as exc:
            logger.error("Destroy of {!r} failed: {}", str(dev_env_name), exc)
            raise click.ClickException(str(exc)) from exc

    _emit_destroy_result(dev_env_name, output_format=output_format)
