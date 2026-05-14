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
from pydantic import SecretStr

from imbue.minds.bootstrap import resolve_minds_root_name
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

_DEV_TIER: Final[str] = "dev"


def _build_real_providers() -> Providers:
    """Wire the provider modules together with the typed callable signatures.

    The provider modules expose keyword-only arguments so the wiring here
    looks a bit ceremonial -- the orchestration uses positional callables
    to keep the test fakes minimal.
    """

    def _create_neon(name: DevEnvName, project_id: str, api_token: SecretStr) -> NeonDatabaseRecord:
        return create_neon_database(name, project_id=project_id, api_token=api_token)

    def _delete_neon(name: DevEnvName, project_id: str, api_token: SecretStr) -> None:
        delete_neon_database(name, project_id=project_id, api_token=api_token)

    def _create_supertokens(name: DevEnvName, core_base_url: str, api_key: SecretStr) -> SuperTokensAppRecord:
        return create_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)

    def _delete_supertokens(name: DevEnvName, core_base_url: str, api_key: SecretStr) -> None:
        delete_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)

    def _list_vultr(name: DevEnvName, api_key: SecretStr) -> tuple[VultrInstanceSummary, ...]:
        return list_vultr_instances(name, api_key=api_key)

    def _delete_vultr(instances: tuple[VultrInstanceSummary, ...], api_key: SecretStr) -> None:
        delete_vultr_instances(instances, api_key=api_key)

    return Providers(
        create_modal_env=create_modal_env,
        delete_modal_env=delete_modal_env,
        create_neon_db=_create_neon,
        delete_neon_db=_delete_neon,
        create_supertokens_app=_create_supertokens,
        delete_supertokens_app=_delete_supertokens,
        list_vultr_instances=_list_vultr,
        delete_vultr_instances=_delete_vultr,
    )


def _load_dev_credentials_from_vault(vault_prefix: str) -> ProviderCredentials:
    """Read every per-provider dev-tier secret from Vault.

    The dev-tier Vault entries follow the shared schema; the specific keys
    consumed by ``minds env`` are pulled out here and fed into the typed
    :class:`ProviderCredentials` model.
    """
    neon = read_vault_kv(VaultPath(f"{vault_prefix}/neon"))
    supertokens = read_vault_kv(VaultPath(f"{vault_prefix}/supertokens"))
    cloudflare_or_misc = read_vault_kv(VaultPath(f"{vault_prefix}/cloudflare"))  # not used yet, but validated to exist
    vultr_secret = read_vault_kv(
        VaultPath(f"{vault_prefix}/pool-ssh")
    )  # pool-ssh entry happens to also carry VULTR_API_KEY in dev

    project_id = neon.get("NEON_PROJECT_ID", "")
    api_token = neon.get("NEON_API_TOKEN", "")
    if not project_id or not api_token:
        raise MindError(
            f"Vault entry {vault_prefix}/neon missing NEON_PROJECT_ID or NEON_API_TOKEN; "
            "add them to the dev-tier Neon secret."
        )

    core_url = supertokens.get("SUPERTOKENS_CONNECTION_URI", "")
    core_api_key = supertokens.get("SUPERTOKENS_API_KEY", "")
    if not core_url or not core_api_key:
        raise MindError(
            f"Vault entry {vault_prefix}/supertokens missing SUPERTOKENS_CONNECTION_URI or SUPERTOKENS_API_KEY."
        )

    vultr_api_key = vultr_secret.get("VULTR_API_KEY", "")
    if not vultr_api_key:
        raise MindError(f"Vault entry {vault_prefix}/pool-ssh missing VULTR_API_KEY (shared dev-tier key for Vultr).")
    _ = cloudflare_or_misc  # asserts the entry exists; not currently consumed.

    return ProviderCredentials(
        neon_project_id=project_id,
        neon_api_token=SecretStr(api_token),
        supertokens_core_url=core_url,
        supertokens_api_key=SecretStr(core_api_key),
        vultr_api_key=SecretStr(vultr_api_key),
    )


def _emit(payload: object, *, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        click.echo(json.dumps(payload, indent=2, default=str))
    elif output_format is OutputFormat.JSONL:
        click.echo(json.dumps(payload, default=str))
    else:
        click.echo(payload)


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

    try:
        credentials = _load_dev_credentials_from_vault(str(deploy_config.vault_path_prefix))
    except VaultReadError as exc:
        raise click.ClickException(str(exc)) from exc

    providers = _build_real_providers()

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


def _emit_create_result(result: CreatedDevEnv, *, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.HUMAN:
        click.echo(f"Created dev env '{result.name}'.")
        click.echo(f"  config:    {result.config_path}")
        click.echo(f"  connector: {result.connector_url}")
        click.echo(f"  litellm:   {result.litellm_proxy_url}")
        click.echo("")
        click.echo("Run `minds run --config-file {}` to launch against this env.".format(result.config_path))
        return
    _emit(
        {
            "name": str(result.name),
            "config_path": result.config_path,
            "connector_url": str(result.connector_url),
            "litellm_proxy_url": str(result.litellm_proxy_url),
        },
        output_format=output_format,
    )


@env.command("list")
@click.pass_context
def env_list(ctx: click.Context) -> None:
    """List the dynamic dev envs configured on this machine."""
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)
    summaries = list_dev_envs()

    if output_format is OutputFormat.HUMAN:
        if not summaries:
            click.echo("No dev envs configured. Run `minds env create <name>` to create one.")
            return
        for s in summaries:
            click.echo(f"{s.name}\t{s.connector_url}\t{s.config_path}")
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
            click.echo(json.dumps(entry, default=str))
    else:
        click.echo(json.dumps(payload, indent=2, default=str))


@env.command("destroy")
@click.argument("name", type=str)
@click.option(
    "--keep-agents",
    is_flag=True,
    default=False,
    help="Reserved -- do not destroy running workspace agents bound to this env.",
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

    try:
        credentials = _load_dev_credentials_from_vault(str(deploy_config.vault_path_prefix))
    except VaultReadError as exc:
        raise click.ClickException(str(exc)) from exc

    providers = _build_real_providers()

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
        # Destroy is best-effort across providers; surface the first failure
        # rather than papering over it (operator can re-run destroy to retry).
        logger.error("Destroy of {!r} failed: {}", str(dev_env_name), exc)
        raise click.ClickException(str(exc)) from exc

    _ = resolve_minds_root_name  # keep the import alive; future hooks may need it
    _emit({"name": str(dev_env_name), "status": "destroyed"}, output_format=output_format)
