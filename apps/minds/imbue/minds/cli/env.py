"""``minds env {activate,deactivate,deploy,destroy,list}``.

Activation is the central UX: ``eval "$(minds env activate <name>)"``
exports the four env vars (``MINDS_ROOT_NAME``, ``MNGR_HOST_DIR``,
``MNGR_PREFIX``, ``MINDS_CLIENT_CONFIG_PATH``) that point the rest of
the stack at the activated env's ``~/.minds-<name>/`` data root.
``minds env deploy`` / ``destroy`` then operate implicitly on whichever
env the shell is activated against -- no env-name argument is accepted,
which keeps "I'm activated against dev env A but accidentally typed
``minds env destroy production``" impossible.

The CLI side constructs real provider callables (Modal CLI / Neon /
SuperTokens / OVH HTTP / Modal deploy) and threads them into the
pure orchestration in :mod:`imbue.minds.envs.provisioning`.

Dev-tier credentials needed for provisioning come from HCP Vault at
command time -- minds never persists them. The operator must already be
logged in to the ``vault`` CLI.
"""

import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Final

import click
import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.cli._activated_env import MODAL_PROFILE_ENV_VAR
from imbue.minds.cli._activated_env import PRODUCTION_ENV_NAME as _PRODUCTION_ENV_NAME
from imbue.minds.cli._activated_env import STAGING_ENV_NAME as _STAGING_ENV_NAME
from imbue.minds.cli._activated_env import modal_profile_for_tier_or_none
from imbue.minds.cli._activated_env import require_activated_env_name
from imbue.minds.cli._activated_env import require_deploy_mode_activation
from imbue.minds.cli._activated_env import tier_for_env_name as _tier_for_env_name
from imbue.minds.cli._activated_env import validate_modal_profile_exists_in_modal_toml
from imbue.minds.cli.pool import tear_down_env_pool_slices
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_client_config
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.config.loader import repo_tier_client_config_path
from imbue.minds.envs.docker_cleanup import cleanup_env_state_container
from imbue.minds.envs.generation import delete_generation_id as real_delete_generation_id
from imbue.minds.envs.generation import ensure_generation_id as real_ensure_generation_id
from imbue.minds.envs.health_check import await_apps_healthy as real_await_apps_healthy
from imbue.minds.envs.local_store import env_root_exists
from imbue.minds.envs.migrations import apply_pool_hosts_migrations as real_apply_pool_hosts_migrations
from imbue.minds.envs.migrations import seed_paid_list_defaults as real_seed_paid_list_defaults
from imbue.minds.envs.mngr_agent_cleanup import destroy_all_mngr_agents_in_env
from imbue.minds.envs.mngr_agent_cleanup import real_destroy_mngr_agents
from imbue.minds.envs.paths import active_env_name_or_none
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.per_env_deploy import build_per_env_secret_values
from imbue.minds.envs.per_env_deploy import delete_modal_secret as real_delete_modal_secret
from imbue.minds.envs.per_env_deploy import deploy_litellm_proxy as real_deploy_litellm_proxy
from imbue.minds.envs.per_env_deploy import deploy_remote_service_connector as real_deploy_remote_service_connector
from imbue.minds.envs.per_env_deploy import ensure_modal_env as real_ensure_modal_env
from imbue.minds.envs.per_env_deploy import get_modal_app_latest_version as real_get_modal_app_latest_version
from imbue.minds.envs.per_env_deploy import push_per_env_modal_secret as real_push_per_env_modal_secret
from imbue.minds.envs.per_env_deploy import rollback_modal_app as real_rollback_modal_app
from imbue.minds.envs.per_env_deploy import stop_modal_app as real_stop_modal_app
from imbue.minds.envs.primitives import DeployStrategy
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import InvalidDevEnvNameError
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.providers.cloudflare_tunnels import delete_tunnels as real_delete_cloudflare_tunnels
from imbue.minds.envs.providers.cloudflare_tunnels import list_tunnels_for_env as real_list_cloudflare_tunnels_for_env
from imbue.minds.envs.providers.modal_env import delete_modal_env as real_delete_modal_env
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.neon_db import create_neon_project
from imbue.minds.envs.providers.neon_db import create_snapshot_branch as real_create_neon_snapshot_branch
from imbue.minds.envs.providers.neon_db import delete_neon_branch as real_delete_neon_branch
from imbue.minds.envs.providers.neon_db import delete_neon_project
from imbue.minds.envs.providers.neon_db import pool_hosts_migrations_dir
from imbue.minds.envs.providers.neon_db import resolve_default_branch_id as real_resolve_neon_default_branch_id
from imbue.minds.envs.providers.neon_db import (
    verify_neon_token_has_restore_scope as real_verify_neon_token_has_restore_scope,
)
from imbue.minds.envs.providers.neon_db import wipe_neon_db_schema as real_wipe_neon_db_schema
from imbue.minds.envs.providers.ovh_tags import OvhCredentials
from imbue.minds.envs.providers.ovh_tags import delete_instances as delete_ovh_instances
from imbue.minds.envs.providers.ovh_tags import list_env_instances as list_ovh_instances
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.supertokens_app import create_supertokens_app
from imbue.minds.envs.providers.supertokens_app import delete_supertokens_app
from imbue.minds.envs.providers.supertokens_app import wipe_supertokens_app_data as real_wipe_supertokens_app_data
from imbue.minds.envs.provisioning import DeployedEnv
from imbue.minds.envs.provisioning import ProviderCredentials
from imbue.minds.envs.provisioning import Providers
from imbue.minds.envs.provisioning import deploy_env
from imbue.minds.envs.provisioning import destroy_env
from imbue.minds.envs.provisioning import list_dev_envs
from imbue.minds.envs.recover import NotInMonorepoError
from imbue.minds.envs.recover import RecoverFailedError
from imbue.minds.envs.recover import RecoverTargetMissingError
from imbue.minds.envs.recover import find_all_recover_target_files
from imbue.minds.envs.recover import find_monorepo_root
from imbue.minds.envs.recover import read_recover_target
from imbue.minds.envs.recover import recover_env
from imbue.minds.envs.recover import recover_target_exists
from imbue.minds.envs.recover import recover_target_path
from imbue.minds.envs.secret_lifecycle import list_modal_secrets as real_list_modal_secrets
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.errors import MindError
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import write_stdout_line
from imbue.mngr_ovh.iam_tags import IamResource

# Reserved env names that map to named tiers; names starting with
# ``ci-`` map to the ``ci`` tier (CI-orchestrator-minted ephemeral envs),
# and everything else maps to the ``dev`` tier. Mirrors the spec's
# hard-coded tier mapping and lets ``minds env deploy`` / ``destroy``
# dispatch on env name alone. The individual ``_PRODUCTION_ENV_NAME`` /
# ``_STAGING_ENV_NAME`` / ``_DEV_TIER`` / ``_CI_TIER`` constants + the
# ``_tier_for_env_name`` mapper live in ``_activated_env.py`` so
# ``minds pool`` (which also needs to derive the tier for its
# Vault-scoped OVH credentials read) can share them without an
# env.py -> pool.py back-reference.
_RESERVED_TIER_ENV_NAMES: Final[frozenset[str]] = frozenset({"production", "staging"})

# Env vars unset by ``deactivate``. Includes every var that any
# activation mode (use-only or ``--deploy``) might have exported, so a
# single ``deactivate`` fully clears the shell regardless of which mode
# was used to activate it. ``MODAL_PROFILE`` is included even though
# plain ``activate`` does not export it, because plain ``activate`` does
# explicitly emit ``unset MODAL_PROFILE`` (so a previously-deploy-
# activated shell flips back cleanly); ``deactivate`` mirrors that.
_ACTIVATION_ENV_VARS: Final[tuple[str, ...]] = (
    MINDS_ROOT_NAME_ENV_VAR,
    "MNGR_HOST_DIR",
    "MNGR_PREFIX",
    "MINDS_CLIENT_CONFIG_PATH",
    # Modal CLI workspace selector. Only set by ``activate --deploy``;
    # see :func:`_build_deploy_mode_exports` and the ``--deploy`` flag
    # on ``minds env activate``.
    MODAL_PROFILE_ENV_VAR,
)


def _ensure_modal_env_for_provider(name: DevEnvName, cg: ConcurrencyGroup) -> None:
    real_ensure_modal_env(name, parent_cg=cg)


def _delete_modal_env_for_provider(name: DevEnvName, cg: ConcurrencyGroup) -> None:
    real_delete_modal_env(name, parent_concurrency_group=cg)


def _create_neon_for_provider(
    name: DevEnvName, org_id: str, api_token: SecretStr, cg: ConcurrencyGroup
) -> NeonProjectRecord:
    return create_neon_project(name, org_id=org_id, api_token=api_token, parent_cg=cg)


def _delete_neon_for_provider(name: DevEnvName, org_id: str, api_token: SecretStr) -> None:
    delete_neon_project(name, org_id=org_id, api_token=api_token)


def _create_supertokens_for_provider(name: DevEnvName, core_base_url: str, api_key: SecretStr) -> SuperTokensAppRecord:
    return create_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)


def _delete_supertokens_for_provider(name: DevEnvName, core_base_url: str, api_key: SecretStr) -> None:
    delete_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)


def _list_ovh_for_provider(name: DevEnvName, credentials: OvhCredentials) -> tuple[IamResource, ...]:
    return list_ovh_instances(name, credentials=credentials)


def _delete_ovh_for_provider(instances: tuple[IamResource, ...], credentials: OvhCredentials) -> None:
    delete_ovh_instances(instances, credentials=credentials)


def _read_per_env_secret_values_for_provider(
    service: str,
    tier_vault_prefix: str,
    overrides: dict[str, str],
    cg: ConcurrencyGroup,
) -> dict[str, str]:
    return build_per_env_secret_values(
        service,
        tier_vault_prefix=tier_vault_prefix,
        overrides=overrides,
        parent_cg=cg,
    )


def _push_per_env_modal_secret_for_provider(
    secret_name: str,
    values: dict[str, str],
    modal_env: str,
    cg: ConcurrencyGroup,
) -> None:
    real_push_per_env_modal_secret(secret_name, values, modal_env=modal_env, parent_cg=cg)


def _deploy_litellm_proxy_for_provider(
    modal_env: str,
    tier: str,
    min_containers: int,
    scaledown_window: int,
    deploy_id: str,
    strategy: DeployStrategy,
    cg: ConcurrencyGroup,
) -> AnyUrl:
    return real_deploy_litellm_proxy(
        modal_env=modal_env,
        tier=tier,
        min_containers=min_containers,
        scaledown_window=scaledown_window,
        deploy_id=deploy_id,
        strategy=strategy,
        parent_cg=cg,
    )


def _deploy_connector_for_provider(
    modal_env: str,
    tier: str,
    min_containers: int,
    scaledown_window: int,
    deploy_id: str,
    strategy: DeployStrategy,
    cg: ConcurrencyGroup,
) -> AnyUrl:
    return real_deploy_remote_service_connector(
        modal_env=modal_env,
        tier=tier,
        min_containers=min_containers,
        scaledown_window=scaledown_window,
        deploy_id=deploy_id,
        strategy=strategy,
        parent_cg=cg,
    )


def _stop_modal_app_for_provider(app_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None:
    real_stop_modal_app(app_name=app_name, modal_env=modal_env, parent_cg=parent_cg)


def _delete_modal_secret_for_provider(secret_name: str, modal_env: str, cg: ConcurrencyGroup) -> None:
    real_delete_modal_secret(secret_name=secret_name, modal_env=modal_env, parent_cg=cg)


def _list_modal_secrets_for_provider(modal_env: str, cg: ConcurrencyGroup) -> tuple[str, ...]:
    return real_list_modal_secrets(modal_env=modal_env, parent_cg=cg)


def _apply_pool_hosts_migrations_for_provider(host_pool_dsn: SecretStr, cg: ConcurrencyGroup) -> tuple[Path, ...]:
    return real_apply_pool_hosts_migrations(host_pool_dsn, migrations_dir=pool_hosts_migrations_dir(), parent_cg=cg)


def _seed_paid_list_defaults_for_provider(
    host_pool_dsn: SecretStr,
    domains: tuple[str, ...],
    emails: tuple[str, ...],
    cg: ConcurrencyGroup,
) -> None:
    real_seed_paid_list_defaults(host_pool_dsn, domains=domains, emails=emails, parent_cg=cg)


def _get_modal_app_latest_version_for_provider(app_name: str, modal_env: str, cg: ConcurrencyGroup) -> str | None:
    return real_get_modal_app_latest_version(app_name=app_name, modal_env=modal_env, parent_cg=cg)


def _rollback_modal_app_for_provider(app_name: str, version: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None:
    real_rollback_modal_app(app_name=app_name, version=version, modal_env=modal_env, parent_cg=parent_cg)


def _create_neon_snapshot_branch_for_provider(
    project_id: str, parent_branch_id: str, name: str, api_token: SecretStr
) -> str:
    return real_create_neon_snapshot_branch(project_id, parent_branch_id, name, api_token=api_token)


def _delete_neon_branch_for_provider(project_id: str, branch_id: str, api_token: SecretStr) -> None:
    real_delete_neon_branch(project_id, branch_id, api_token=api_token)


def _resolve_neon_default_branch_id_for_provider(project_id: str, api_token: SecretStr) -> str:
    return real_resolve_neon_default_branch_id(project_id, api_token=api_token)


def _verify_neon_token_has_restore_scope_for_provider(project_id: str, api_token: SecretStr) -> None:
    real_verify_neon_token_has_restore_scope(project_id, api_token=api_token)


def _await_apps_healthy_for_provider(connector_url: AnyUrl, litellm_proxy_url: AnyUrl) -> None:
    real_await_apps_healthy(connector_url=connector_url, litellm_proxy_url=litellm_proxy_url)


def _wipe_supertokens_for_provider(app_id: str, core_base_url: str, api_key: SecretStr) -> None:
    real_wipe_supertokens_app_data(app_id, core_base_url=core_base_url, api_key=api_key)


def _wipe_neon_db_schema_for_provider(dsn: SecretStr, cg: ConcurrencyGroup) -> None:
    real_wipe_neon_db_schema(dsn, parent_cg=cg)


def _ensure_generation_id_for_provider(tier_vault_prefix: str, cg: ConcurrencyGroup) -> str:
    return real_ensure_generation_id(tier_vault_prefix, parent_concurrency_group=cg)


def _delete_generation_id_for_provider(tier_vault_prefix: str, cg: ConcurrencyGroup) -> None:
    real_delete_generation_id(tier_vault_prefix, parent_concurrency_group=cg)


def _cleanup_state_container_for_provider(name: DevEnvName, cg: ConcurrencyGroup) -> None:
    cleanup_env_state_container(name, parent_concurrency_group=cg)


def _list_cloudflare_tunnels_for_env_for_provider(
    name: DevEnvName, account_id: str, api_token: SecretStr
) -> tuple[str, ...]:
    return real_list_cloudflare_tunnels_for_env(name, account_id=account_id, api_token=api_token)


def _delete_cloudflare_tunnels_for_provider(
    tunnel_ids: tuple[str, ...], account_id: str, api_token: SecretStr
) -> None:
    real_delete_cloudflare_tunnels(tunnel_ids, account_id=account_id, api_token=api_token)


def _build_real_providers() -> Providers:
    """Wire the provider modules into the Providers bundle.

    Every callable here takes a :class:`ConcurrencyGroup` so subprocess
    work (Modal CLI shellouts, vault reads) is tracked by the group the
    CLI command brackets the whole deploy in.
    """
    return Providers(
        ensure_modal_env=_ensure_modal_env_for_provider,
        delete_modal_env=_delete_modal_env_for_provider,
        create_neon_project=_create_neon_for_provider,
        delete_neon_project=_delete_neon_for_provider,
        create_supertokens_app=_create_supertokens_for_provider,
        delete_supertokens_app=_delete_supertokens_for_provider,
        list_ovh_instances=_list_ovh_for_provider,
        delete_ovh_instances=_delete_ovh_for_provider,
        read_per_env_secret_values=_read_per_env_secret_values_for_provider,
        push_per_env_modal_secret=_push_per_env_modal_secret_for_provider,
        deploy_litellm_proxy=_deploy_litellm_proxy_for_provider,
        deploy_remote_service_connector=_deploy_connector_for_provider,
        stop_modal_app=_stop_modal_app_for_provider,
        delete_modal_secret=_delete_modal_secret_for_provider,
        list_modal_secrets=_list_modal_secrets_for_provider,
        apply_pool_hosts_migrations=_apply_pool_hosts_migrations_for_provider,
        seed_paid_list_defaults=_seed_paid_list_defaults_for_provider,
        get_modal_app_latest_version=_get_modal_app_latest_version_for_provider,
        rollback_modal_app=_rollback_modal_app_for_provider,
        create_neon_snapshot_branch=_create_neon_snapshot_branch_for_provider,
        delete_neon_branch=_delete_neon_branch_for_provider,
        resolve_neon_default_branch_id=_resolve_neon_default_branch_id_for_provider,
        verify_neon_token_has_restore_scope=_verify_neon_token_has_restore_scope_for_provider,
        await_apps_healthy=_await_apps_healthy_for_provider,
        destroy_mngr_agents=real_destroy_mngr_agents,
        cleanup_state_container=_cleanup_state_container_for_provider,
        wipe_supertokens_app_data=_wipe_supertokens_for_provider,
        wipe_neon_db_schema=_wipe_neon_db_schema_for_provider,
        ensure_generation_id=_ensure_generation_id_for_provider,
        delete_generation_id=_delete_generation_id_for_provider,
        list_cloudflare_tunnels_for_env=_list_cloudflare_tunnels_for_env_for_provider,
        delete_cloudflare_tunnels=_delete_cloudflare_tunnels_for_provider,
    )


def _load_dev_credentials_from_vault(vault_prefix: str, *, cg: ConcurrencyGroup) -> ProviderCredentials:
    """Read every per-provider dev-tier credential `minds env` needs from Vault.

    These credentials live in dedicated "admin" Vault entries that are
    intentionally separate from the Modal-pushed entries -- the connector's
    runtime never needs API tokens for creating Neon DBs or VPS instances,
    so co-mingling those tokens with the Modal-pushed Vault paths would
    leak them into the connector's runtime env unnecessarily.

    Paths read here (none are pushed to Modal):

    - ``<vault_prefix>/neon-admin`` -- ``NEON_API_TOKEN`` (required),
      ``NEON_ORG_ID`` (required for dev tier where projects are created),
      ``NEON_PROJECT_ID`` (required for shared tiers where the operator
      brings a pre-existing project; used by the deploy's pre-deploy
      snapshot + recover's restore-from-snapshot calls).
    - ``<vault_prefix>/supertokens`` -- ``SUPERTOKENS_CONNECTION_URI``,
      ``SUPERTOKENS_API_KEY`` (read from the Modal-pushed entry; safe to
      read here because the connector also legitimately needs both keys)
    - ``<vault_prefix>/ovh`` -- ``OVH_APPLICATION_KEY``, ``OVH_APPLICATION_SECRET``,
      ``OVH_CONSUMER_KEY``
    """
    neon_admin = read_vault_kv(VaultPath(f"{vault_prefix}/neon-admin"), parent_concurrency_group=cg)
    supertokens = read_vault_kv(VaultPath(f"{vault_prefix}/supertokens"), parent_concurrency_group=cg)
    # The ovh entry is optional -- a tier with no OVH provisioning yet may not
    # have it populated. Treat a genuinely *missing* entry as empty so the
    # deploy still progresses; per-env OVH-touching operations will fail later
    # if/when the operator wires them up without populating Vault. Only catch
    # VaultSecretNotFoundError here: a transient/auth VaultReadError must NOT be
    # silently turned into empty OVH credentials (that would deploy a broken
    # `ovh` Modal Secret on a Vault blip), so let those propagate.
    try:
        ovh_secret = read_vault_kv(VaultPath(f"{vault_prefix}/ovh"), parent_concurrency_group=cg)
    except VaultSecretNotFoundError as exc:
        logger.warning("No ovh Vault entry at {}/ovh ({}); proceeding with empty OVH credentials.", vault_prefix, exc)
        ovh_secret = {}

    org_id = neon_admin.get("NEON_ORG_ID", "")
    api_token = neon_admin.get("NEON_API_TOKEN", "")
    project_id_raw = neon_admin.get("NEON_PROJECT_ID", "")
    if not api_token:
        raise VaultReadError(
            f"Vault entry {vault_prefix}/neon-admin missing NEON_API_TOKEN; "
            "see .minds/template/neon-admin.sh for the schema."
        )
    # ``NEON_ORG_ID`` is only required when the deploy creates the Neon
    # project (dev tier). ``NEON_PROJECT_ID`` is only required when the
    # deploy adopts an existing one (shared tiers). Either one populated
    # is enough at the credential-load layer; deploy_env enforces the
    # right one for its tier via ``deploy_config.lifecycle.creates_resources``.
    if not org_id and not project_id_raw:
        raise VaultReadError(
            f"Vault entry {vault_prefix}/neon-admin missing both NEON_ORG_ID and NEON_PROJECT_ID. "
            "Dev tier needs NEON_ORG_ID; shared tiers (staging / production) need NEON_PROJECT_ID. "
            "See .minds/template/neon-admin.sh for the schema."
        )
    neon_project_id: str | None = project_id_raw if project_id_raw else None

    core_url = supertokens.get("SUPERTOKENS_CONNECTION_URI", "")
    core_api_key = supertokens.get("SUPERTOKENS_API_KEY", "")
    if not core_url or not core_api_key:
        raise VaultReadError(
            f"Vault entry {vault_prefix}/supertokens missing SUPERTOKENS_CONNECTION_URI or SUPERTOKENS_API_KEY."
        )

    return ProviderCredentials(
        neon_org_id=org_id,
        neon_api_token=SecretStr(api_token),
        neon_project_id=neon_project_id,
        supertokens_core_url=core_url,
        supertokens_api_key=SecretStr(core_api_key),
        ovh_credentials=OvhCredentials(
            application_key=SecretStr(ovh_secret.get("OVH_APPLICATION_KEY", "")),
            application_secret=SecretStr(ovh_secret.get("OVH_APPLICATION_SECRET", "")),
            consumer_key=SecretStr(ovh_secret.get("OVH_CONSUMER_KEY", "")),
        ),
    )


def _emit_json(payload: object, *, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        write_stdout_line(json.dumps(payload, indent=2, default=str))
    elif output_format is OutputFormat.JSONL:
        write_stdout_line(json.dumps(payload, default=str))
    else:
        write_stdout_line(str(payload))


def _emit_deploy_result(result: DeployedEnv, *, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.HUMAN:
        logger.info("Deployed env '{}' (tier '{}') into Modal env '{}'.", result.name, result.tier, result.modal_env)
        if result.client_config_path is not None:
            logger.info("  client.toml:  {}", result.client_config_path)
        if result.secrets_path is not None:
            logger.info("  secrets.toml: {}", result.secrets_path)
        logger.info("  connector:    {}", result.connector_url)
        logger.info("  litellm:      {}", result.litellm_proxy_url)
        if result.client_config_path is not None:
            logger.info("Run `minds run` (with this env still activated) to launch against it.")
        else:
            logger.info(
                "Update `apps/minds/imbue/minds/config/envs/{}/client.toml` if these URLs "
                "differ from what's committed, and open a PR.",
                result.tier,
            )
        return
    _emit_json(
        {
            "name": str(result.name),
            "tier": result.tier,
            "modal_env": result.modal_env,
            "client_config_path": result.client_config_path,
            "secrets_path": result.secrets_path,
            "connector_url": str(result.connector_url),
            "litellm_proxy_url": str(result.litellm_proxy_url),
        },
        output_format=output_format,
    )


_AUTO_ROLLBACK_COUNTDOWN_SECONDS: Final[int] = 5


def _exec_into_recover(*, deploy_error: Exception) -> None:
    """Replace the current process with ``minds env recover``.

    Called from the deploy-failure path so the operator doesn't have to
    re-type the follow-up command. Uses the process-replacement primitive
    so the recover process INHERITS our stdout/stderr and exit code --
    from the operator's shell it looks like one command (``minds env
    deploy``) that just kept running through the rollback. We pass
    ``--from-failed-deploy`` so that inherited exit code is non-zero even
    when the rollback succeeds: the deploy failed, and a clean rollback
    must not let a caller / CI read that failure as success.

    The first argv element comes from ``sys.argv[0]`` so the same
    minds binary (or ``uv run minds`` wrapper) the operator launched
    picks up the recover subcommand. Anything currently in the
    ConcurrencyGroup gets abandoned -- by the time we reach this path
    the deploy has already hit a definitive failure, so any subprocess
    work is complete + there's nothing to clean up.

    A short visible countdown gives the operator a chance to Ctrl-C
    if the failure was something they can fix in place without a full
    rollback (e.g. a transient cloud blip, a missing Vault entry that
    they can populate + retry). Ctrl-C during the countdown leaves the
    recover-target file on disk so the next deploy refuses + the
    operator can decide whether to run recover manually or delete the
    file.
    """
    logger.error("Deploy failed: {}", deploy_error)
    logger.error(
        "Will auto-run `minds env recover` in {} seconds to roll back to the pre-deploy state. "
        "Press Ctrl-C to cancel + run recover manually (or delete the recover-target file).",
        _AUTO_ROLLBACK_COUNTDOWN_SECONDS,
    )
    sys.stderr.flush()
    for remaining in range(_AUTO_ROLLBACK_COUNTDOWN_SECONDS, 0, -1):
        logger.warning("Rolling back in {}...", remaining)
        time.sleep(1)
    logger.info("Running `minds env recover` now.")
    # Flush before exec -- otherwise buffered loguru output gets lost.
    sys.stdout.flush()
    sys.stderr.flush()
    argv0 = sys.argv[0]
    # `--from-failed-deploy` makes recover exit non-zero even when the rollback
    # succeeds, so the deploy failure that triggered this is never masked by a
    # clean rollback's exit code (we exec, so recover's exit code becomes ours).
    os.execvp(argv0, [argv0, "env", "recover", "--from-failed-deploy"])


def _refuse_if_any_recover_target_exists() -> None:
    """Block the command if ANY per-env recover-target file sits at the monorepo root.

    Used by env-agnostic commands (``deactivate``, ``list``) -- they
    don't have a single env in mind, but we still want to surface any
    in-flight failed deploy that the operator might have forgotten
    about. Each matching file's env name is listed in the error so the
    operator knows which env(s) need ``minds env recover``. (``activate``
    uses the env-scoped :func:`_refuse_if_recover_target_blocks_activation`
    instead, so it can still activate an env in order to recover it.)

    Tolerates ``NotInMonorepoError`` for commands that don't strictly
    require monorepo context (e.g. ``list`` from $HOME); when we can't
    find a monorepo root we can't have a recover-target there either.
    """
    try:
        repo_root = find_monorepo_root()
    except NotInMonorepoError:
        # No monorepo root reachable means no recover-target file can be
        # there; tolerate it. Any other MindError propagates.
        logger.debug("Not inside the monorepo; skipping recover-target check")
        return
    files = find_all_recover_target_files(repo_root=repo_root)
    if not files:
        return
    file_list = "\n".join(f"  - {f.name}" for f in files)
    raise click.ClickException(
        f"{len(files)} recover-target file(s) sit at {repo_root} -- one or more prior "
        f"`minds env deploy` runs failed mid-flight:\n{file_list}\n"
        "Activate each affected env and run `minds env recover` (or delete a known-stale file "
        "manually) before any other minds env command."
    )


def _refuse_if_this_env_recover_target_exists(env_name: str) -> None:
    """Block the command if THIS env's recover-target file exists.

    Used by env-scoped commands (``deploy``, ``destroy``) so a
    leftover recover-target for a DIFFERENT env doesn't block this
    env's operation -- per-env naming is the whole point of F26's
    file-rename, supporting test parallelism where each test mints
    its own env name.
    """
    try:
        repo_root = find_monorepo_root()
    except NotInMonorepoError:
        # No monorepo root reachable means no recover-target file can be
        # there; tolerate it. Any other MindError propagates.
        logger.debug("Not inside the monorepo; skipping recover-target check")
        return
    if recover_target_exists(repo_root=repo_root, env_name=env_name):
        raise click.ClickException(
            f"Recover-target file exists at {recover_target_path(repo_root=repo_root, env_name=env_name)} -- "
            f"a prior `minds env deploy` against env {env_name!r} failed mid-flight. Run `minds env recover` "
            "(with this env activated) to roll back, or delete the file manually if it's known-stale."
        )


def _refuse_if_recover_target_blocks_activation(env_name: str) -> None:
    """Block activating ``env_name`` only when a DIFFERENT env's recover-target exists.

    ``minds env recover`` requires an *activated* env, so the blanket
    "refuse activation while ANY recover-target exists" guard created a
    catch-22: a failed deploy's own recover-target sat at the monorepo
    root and made it impossible to activate that env in order to recover
    it. Instead we allow activating an env that has its own pending
    recover-target (the activate-then-recover path), and only hard-refuse
    when the pending recover-target(s) belong solely to *other* envs -- a
    forgotten failed deploy the operator should clear first. When
    activating an affected env, any other envs' targets are surfaced as a
    warning rather than blocking.
    """
    try:
        repo_root = find_monorepo_root()
    except NotInMonorepoError:
        # No monorepo root reachable from cwd means there can't be a
        # recover-target file there either, so there's nothing to guard
        # against. Any other MindError is a real failure and propagates.
        logger.debug("Not inside the monorepo; skipping recover-target activation guard")
        return
    files = find_all_recover_target_files(repo_root=repo_root)
    if not files:
        return
    this_env_path = recover_target_path(repo_root=repo_root, env_name=env_name)
    other_files = [recover_file for recover_file in files if recover_file != this_env_path]
    if this_env_path in files:
        # Activating the very env that needs recovery: allow it so `minds
        # env recover` can run next. Surface any unrelated failed deploys.
        if other_files:
            other_list = "\n".join(f"  - {recover_file.name}" for recover_file in other_files)
            logger.warning(
                "Other env(s) still have a pending recover-target file:\n{}\n"
                "Activate each and run `minds env recover` before deploying them.",
                other_list,
            )
        return
    file_list = "\n".join(f"  - {recover_file.name}" for recover_file in other_files)
    raise click.ClickException(
        f"{len(other_files)} recover-target file(s) for other env(s) sit at {repo_root} -- one or "
        f"more prior `minds env deploy` runs failed mid-flight:\n{file_list}\n"
        "Activate each affected env and run `minds env recover` (or delete a known-stale file "
        "manually) before activating an unaffected env."
    )


def _emit_destroy_result(env_name: str, *, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.HUMAN:
        logger.info("Destroyed dev env '{}'.", env_name)
        logger.info(
            'Your shell is still activated against "{}". Clear it with: eval "$(uv run minds env deactivate)"',
            env_name,
        )
        return
    _emit_json({"name": env_name, "status": "destroyed"}, output_format=output_format)


@click.group()
def env() -> None:
    """Manage minds environments (dev / staging / production)."""


@env.command("activate")
@click.argument("name", type=str)
@click.option(
    "--create",
    is_flag=True,
    default=False,
    help=(
        "Idempotently create ``~/.minds-<name>/`` if it doesn't exist before activating. "
        "Without this flag, activate refuses for dev env names whose env root is missing "
        "(so a typo doesn't silently materialize a wrong directory). Use this for "
        "the first activation of a fresh dev env, then `minds env deploy` to populate "
        "it. No-op when the dir already exists, or when ``NAME`` is a reserved tier "
        "name (`staging` / `production` always auto-create)."
    ),
)
@click.option(
    "--deploy",
    "is_deploy_mode",
    is_flag=True,
    default=False,
    help=(
        "Activate in deploy mode: in addition to the use-side env vars (MINDS_ROOT_NAME, "
        "MNGR_HOST_DIR, MNGR_PREFIX, MINDS_CLIENT_CONFIG_PATH), export MODAL_PROFILE pinned "
        "to the tier's modal_workspace from deploy.toml. Required for `minds env deploy`, "
        "`minds env destroy`, and `minds env recover`. Without this flag (the default), "
        "activate emits `unset MODAL_PROFILE` so a previously-deploy-activated shell flips "
        "back to use-only -- the rest of the stack (mngr, minds run, Latchkey) no longer "
        "tries to authenticate against a Modal workspace the operator may not have tokens "
        "for. Fails up front if `~/.modal.toml` has no profile matching the tier's "
        "modal_workspace (run `modal token set --profile <workspace>` first)."
    ),
)
def env_activate(name: str, create: bool, is_deploy_mode: bool) -> None:
    """Print shell exports that activate env ``NAME`` in the calling shell.

    Refuses when a recover-target file exists at the monorepo root --
    a prior failed deploy must be cleared via ``minds env recover``
    before any other env command runs.

    Designed for ``eval "$(uv run minds env activate <name>)"``: after
    sourcing, ``mngr`` writes to ``~/.minds-<name>/mngr``, ``minds run``
    picks up the per-env client config without a ``--config-file`` flag.
    ``minds env deploy`` / ``destroy`` / ``recover`` additionally require
    ``--deploy`` activation (see below).

    Activation modes:

    - **Use-only (default)**: exports the four use-side env vars and
      emits ``unset MODAL_PROFILE``. Lets the operator run the desktop
      client, browse agents, hit Latchkey, etc. against the activated
      env without touching their Modal CLI auth state. This is what
      every non-deploying user wants.
    - **Deploy mode (``--deploy``)**: additionally exports
      ``MODAL_PROFILE=<tier's modal_workspace>`` so every subsequent
      ``modal`` shellout (``modal deploy``, ``modal secret create``,
      etc.) targets the right Modal account, regardless of which profile
      is marked ``active = true`` in ``~/.modal.toml``. Pre-validates
      that ``~/.modal.toml`` has a matching profile and refuses
      otherwise (with a ``modal token set --profile <workspace>`` hint).

    Emitted use-side variables (both modes):

    - ``MINDS_ROOT_NAME`` -- ``minds`` for the reserved ``production``
      name, ``minds-<name>`` for every other env. Validation runs at
      activation time, so a typo in ``<name>`` fails here instead of
      silently exporting nonsense.
    - ``MNGR_HOST_DIR`` -- the env's mngr profile (``~/.minds-<name>/mngr``).
    - ``MNGR_PREFIX`` -- the env's mngr-resource prefix
      (``minds-<name>-``).
    - ``MINDS_CLIENT_CONFIG_PATH`` -- the in-repo
      ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` for
      ``staging`` / ``production`` (the source of truth for those tiers,
      committed); the per-env ``~/.minds-<name>/client.toml`` for dev
      envs (written by ``minds env deploy``).

    Behaviour by env type:

    - ``production`` / ``staging``: always auto-creates the env root if
      it does not exist (they're reserved names -- no typo risk). The
      in-repo ``client.toml`` for the tier must already exist (it ships
      with the repo).
    - Any other name: validated via :class:`DevEnvName`. Refuses to
      activate when ``~/.minds-<name>/`` does not exist *unless*
      ``--create`` is passed -- which idempotently mkdirs the env root
      and proceeds. Without ``--create``, the error message tells the
      operator how to bootstrap a fresh dev env in one line.
    """
    _refuse_if_recover_target_blocks_activation(name)

    if name in _RESERVED_TIER_ENV_NAMES or name == _PRODUCTION_ENV_NAME:
        _activate_reserved_env(name, is_deploy_mode=is_deploy_mode)
        return

    try:
        dev_env_name = DevEnvName(name)
    except InvalidDevEnvNameError as exc:
        raise click.ClickException(str(exc)) from exc

    target = env_root_dir(dev_env_name)
    if not env_root_exists(dev_env_name):
        if not create:
            raise click.ClickException(
                f"No env root for {name!r} at {target}. "
                f"For a fresh dev env: re-run with --create -- "
                f'`eval "$(uv run minds env activate --create {name})"` -- '
                "then `uv run minds env deploy` to populate it."
            )
        # Idempotent mkdir: parents=True so a fresh $HOME also works,
        # exist_ok=True is redundant with the env_root_exists check but
        # cheap insurance against a race.
        target.mkdir(parents=True, exist_ok=True)

    root_name = root_name_for_env_name(name)
    config_path = client_config_file(dev_env_name)
    use_side_exports = {
        MINDS_ROOT_NAME_ENV_VAR: root_name,
        "MNGR_HOST_DIR": str(mngr_host_dir_for(root_name)),
        "MNGR_PREFIX": mngr_prefix_for(root_name),
        "MINDS_CLIENT_CONFIG_PATH": str(config_path),
    }
    deploy_exports, deploy_unsets = _build_deploy_mode_exports(
        tier=_tier_for_env_name(name), is_deploy_mode=is_deploy_mode
    )
    # Check the tier generation id + auto-wipe local state on mismatch.
    # Skipped silently when the per-env client.toml doesn't exist yet
    # (fresh `activate --create` before the first deploy) -- the deploy
    # then writes both client.toml and an initial generation marker on
    # success, so subsequent activations have something to compare against.
    if config_path.is_file():
        _try_run_generation_check(env_name=name, client_config_path=config_path, env_root=target)
    _print_activation_exports(
        name=name,
        exports={**use_side_exports, **deploy_exports},
        unsets=deploy_unsets,
        is_deploy_mode=is_deploy_mode,
    )


def _activate_reserved_env(name: str, *, is_deploy_mode: bool) -> None:
    """Activate ``staging`` or ``production`` -- in-repo client.toml is the truth.

    Auto-creates the env root if missing so subsequent commands have
    somewhere to write runtime state (mngr profile, auth, agents).
    Verifies the committed in-repo ``client.toml`` exists -- otherwise
    activation would silently point at a non-existent file and the next
    ``minds run`` would fail with a confusing "Cannot read client
    config" error.
    """
    if name == _PRODUCTION_ENV_NAME:
        root_name = DEFAULT_MINDS_ROOT_NAME
        repo_client = repo_tier_client_config_path(_PRODUCTION_ENV_NAME)
    else:
        assert name == _STAGING_ENV_NAME
        root_name = root_name_for_env_name(_STAGING_ENV_NAME)
        repo_client = repo_tier_client_config_path(_STAGING_ENV_NAME)

    if not repo_client.is_file():
        raise click.ClickException(
            f"In-repo {name} client config not found at {repo_client}. "
            f"This file is committed for {name}; check your monorepo checkout."
        )

    # Create the env root if missing. Safe because the names are
    # reserved -- no typo can land here.
    mngr_host = mngr_host_dir_for(root_name)
    mngr_host.parent.mkdir(parents=True, exist_ok=True)

    use_side_exports = {
        MINDS_ROOT_NAME_ENV_VAR: root_name,
        "MNGR_HOST_DIR": str(mngr_host),
        "MNGR_PREFIX": mngr_prefix_for(root_name),
        "MINDS_CLIENT_CONFIG_PATH": str(repo_client),
    }
    deploy_exports, deploy_unsets = _build_deploy_mode_exports(tier=name, is_deploy_mode=is_deploy_mode)
    # Generation-id check applies to staging (the shared tier where
    # destroy/redeploy by one dev outdates everyone's local state).
    # Production destroy is hard-refused so a mismatch there is
    # impossible -- skip the network round-trip.
    if name == _STAGING_ENV_NAME:
        env_root = mngr_host.parent
        _try_run_generation_check(env_name=name, client_config_path=repo_client, env_root=env_root)
    _print_activation_exports(
        name=name,
        exports={**use_side_exports, **deploy_exports},
        unsets=deploy_unsets,
        is_deploy_mode=is_deploy_mode,
    )


def _build_deploy_mode_exports(*, tier: str, is_deploy_mode: bool) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return ``(exports, unsets)`` for the deploy-side activation knobs.

    Use-only activation (the default) emits ``unset MODAL_PROFILE`` so a
    previously-deploy-activated shell flips back cleanly; deploy-mode
    activation emits ``export MODAL_PROFILE=<workspace>`` after
    validating that ``~/.modal.toml`` has a matching profile. Tiers
    whose ``deploy.toml`` has no committed ``modal_workspace`` (or the
    literal ``CHANGE_ME`` placeholder) skip both the export and the
    unset -- there is no workspace to pin to, so plain-activate
    inheritance of an operator-set ``MODAL_PROFILE`` stays the operator's
    own concern.
    """
    workspace = modal_profile_for_tier_or_none(tier)
    if workspace is None:
        return {}, ()
    if not is_deploy_mode:
        return {}, (MODAL_PROFILE_ENV_VAR,)
    validate_modal_profile_exists_in_modal_toml(workspace)
    return {MODAL_PROFILE_ENV_VAR: workspace}, ()


def _print_activation_exports(
    *,
    name: str,
    exports: dict[str, str],
    unsets: tuple[str, ...] = (),
    is_deploy_mode: bool = False,
) -> None:
    # Mirror the invocation mode in the header so a user who copy-pastes
    # the suggested re-source command back into a fresh shell lands in
    # the same mode they started in -- otherwise re-sourcing a `--deploy`
    # activation via the header would silently drop MODAL_PROFILE and
    # trip the deploy-mode gate on the next minds env deploy/destroy.
    deploy_flag = " --deploy" if is_deploy_mode else ""
    write_stdout_line(f'# Activated env {name!r}. Source via: eval "$(uv run minds env activate{deploy_flag} {name})"')
    for key, value in exports.items():
        write_stdout_line(f"export {key}={shlex.quote(value)}")
    for key in unsets:
        write_stdout_line(f"unset {key}")


# Trailing marker file name under each env root. Stores the generation
# id the dev last saw on this machine for this env, so subsequent
# activations can detect a tier destroy + redeploy and auto-wipe stale
# local state. Lives at ``~/.minds-<env-name>/last_seen_generation``.
_LAST_SEEN_GENERATION_FILE: Final[str] = "last_seen_generation"

# Local-state subdirs the auto-wipe nukes on generation mismatch.
# Picked to clear everything that points at the (now-gone) old tier:
# mngr profile (agents, host config, provider sessions), auth (one-time
# codes, signing key), logs (stale event JSONLs that would seed the
# next UI with vanished workspaces). The env root itself stays so
# subsequent commands have somewhere to write.
_AUTO_WIPED_LOCAL_STATE_SUBDIRS: Final[tuple[str, ...]] = ("mngr", "auth", "logs")


def _try_run_generation_check(*, env_name: str, client_config_path: Path, env_root: Path) -> None:
    """Load ``client_config_path`` -> call the generation check, swallow / log on errors.

    Wrapped here so the activate path never blocks on a misconfigured
    client config: a parse failure surfaces as a warning + skip rather
    than tanking activation.
    """
    try:
        client_config = load_client_config(client_config_path)
    except EnvConfigError as exc:
        logger.warning(
            "Could not load {} for generation-id check ({}); skipping the check.",
            client_config_path,
            exc,
        )
        return
    _check_generation_id_and_wipe_local_state_on_mismatch(
        env_name=env_name,
        connector_url=str(client_config.connector_url).rstrip("/"),
        env_root=env_root,
    )


def _destroy_agents_and_state_container_for_wipe(env_name: str) -> None:
    """Destroy the env's mngr agents + remove its Docker state container.

    Run from the activate-time auto-wipe, *before* the local profile is
    rmtree'd, so the freed Docker resources (host containers, the singleton
    state container) don't outlive the env. Errors are NOT swallowed: a
    teardown failure must surface so the operator can fix it instead of
    silently leaking containers.
    """
    dev_env_name = DevEnvName(env_name)
    with ConcurrencyGroup(name=f"minds-env-wipe-{env_name}") as cg:
        destroyed_count = destroy_all_mngr_agents_in_env(
            dev_env_name,
            destroy_agents=real_destroy_mngr_agents,
            parent_concurrency_group=cg,
        )
        if destroyed_count:
            logger.info("Destroyed {} mngr agent(s) during wipe of env {!r}", destroyed_count, env_name)
        cleanup_env_state_container(dev_env_name, parent_concurrency_group=cg)


def _check_generation_id_and_wipe_local_state_on_mismatch(
    *,
    env_name: str,
    connector_url: str,
    env_root: Path,
) -> None:
    """Fetch ``<connector_url>/generation`` and wipe local state on mismatch.

    Writes diagnostics to stderr (stdout is reserved for shell-sourceable
    exports). Network or parse errors are logged as warnings and do NOT
    block activation -- the operator can still go through the rest of
    the activation, and the next time they activate with a working
    connector the marker will get reconciled.

    Auto-wipe scope: removes the contents of the subdirs in
    :data:`_AUTO_WIPED_LOCAL_STATE_SUBDIRS` under ``env_root``. Anything
    else under the env root (the env's own ``client.toml`` /
    ``secrets.toml`` for dev envs, plus the new ``last_seen_generation``
    marker we're about to write) survives so the env stays usable for
    the operator's next ``mngr create`` / ``minds run``.
    """
    marker_path = env_root / _LAST_SEEN_GENERATION_FILE
    last_seen = marker_path.read_text().strip() if marker_path.is_file() else ""

    url = connector_url.rstrip("/") + "/generation"
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Could not fetch {} ({}); skipping generation-id check. Local state may be stale "
            "if the tier was destroyed since you last activated.",
            url,
            exc,
        )
        return

    current = payload.get("generation_id") if isinstance(payload, dict) else None
    if not isinstance(current, str) or not current:
        # Connector exposed an empty id, meaning the deploy never pushed
        # one (e.g. pre-generation-lifecycle deploy). Skip the wipe;
        # operator can re-deploy to start tracking generations.
        return

    if last_seen == current:
        # Already up to date. Refresh the marker mtime so file-system
        # debug tools show a fresh timestamp, but no wipe needed.
        return

    if last_seen:
        logger.warning(
            "Env {!r} generation id changed (was {}, now {}). Wiping local mngr/auth/logs "
            "state under {} so this shell doesn't see stale agents pointing at the previous "
            "deploy.",
            env_name,
            last_seen,
            current,
            env_root,
        )
        # Tear down the env's now-stale mngr agents + its Docker state
        # container BEFORE wiping the local profile: destroying agents removes
        # their host containers (and build images) via mngr, and the state
        # container must be removed while the profile it derives user_id from
        # still exists.
        _destroy_agents_and_state_container_for_wipe(env_name)
        for subdir in _AUTO_WIPED_LOCAL_STATE_SUBDIRS:
            target = env_root / subdir
            if target.exists():
                shutil.rmtree(target)

    # Write the new marker -- both for the first-time-activation case
    # (no marker yet) and after a wipe.
    env_root.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(current + "\n")


@env.command("deactivate")
def env_deactivate() -> None:
    """Print the ``unset`` exports that deactivate the current env.

    Symmetric with :func:`env_activate`. Use as
    ``eval "$(uv run minds env deactivate)"``. After sourcing, the shell
    has no activated env -- ``minds run`` refuses to start until you
    activate something, and ``mngr`` falls back to its own
    ``~/.mngr/`` default.
    """
    _refuse_if_any_recover_target_exists()
    write_stdout_line('# Deactivate the current env. Source via: eval "$(uv run minds env deactivate)"')
    for key in _ACTIVATION_ENV_VARS:
        write_stdout_line(f"unset {key}")


@env.command("list")
@click.pass_context
def env_list(ctx: click.Context) -> None:
    """List every minds env on disk (every ``~/.minds*/`` dir)."""
    _refuse_if_any_recover_target_exists()
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)
    summaries = list_dev_envs()
    active = active_env_name_or_none()

    if output_format is OutputFormat.HUMAN:
        if not summaries:
            logger.info("No minds env roots found under {}.", Path.home())
            logger.info('Run `eval "$(uv run minds env activate <name>)"` to create one.')
            return
        for s in summaries:
            marker = " (active)" if s.name == active else ""
            connector = str(s.connector_url) if s.connector_url is not None else "(no client.toml)"
            if s.client_config_path is None:
                client_loc = "(no client.toml — run `minds env deploy`)"
            elif s.client_config_source == "in_repo":
                client_loc = f"{s.client_config_path}  (in-repo, committed)"
            else:
                client_loc = s.client_config_path
            logger.info("{}{}\t{}\t{}\t{}", s.name, marker, s.env_root, connector, client_loc)
        return

    payload = [
        {
            "name": s.name,
            "env_root": s.env_root,
            "client_config_path": s.client_config_path,
            "client_config_source": s.client_config_source,
            "connector_url": str(s.connector_url) if s.connector_url is not None else None,
            "is_active": s.name == active,
        }
        for s in summaries
    ]
    if output_format is OutputFormat.JSONL:
        for entry in payload:
            write_stdout_line(json.dumps(entry, default=str))
    else:
        write_stdout_line(json.dumps(payload, indent=2, default=str))


@env.command("deploy")
@click.option(
    "--yes-i-mean-production",
    is_flag=True,
    default=False,
    help=(
        "Required confirmation for deploying against the production tier. "
        "Refuses without it so an accidental `minds env deploy` while "
        "activated against production can never silently fire."
    ),
)
@click.option(
    "--yes-i-mean-staging",
    is_flag=True,
    default=False,
    help="Required confirmation for deploying against the staging tier. Mirrors --yes-i-mean-production.",
)
@click.option(
    "--hard",
    "hard",
    is_flag=True,
    default=False,
    help=(
        "Force `modal deploy --strategy=recreate`: terminate all running containers "
        "so the next request cold-boots a fresh container at the new version. "
        "Mutually exclusive with --soft. Brief downtime / latency window; every "
        "subsequent request is guaranteed to hit the new code."
    ),
)
@click.option(
    "--soft",
    "soft",
    is_flag=True,
    default=False,
    help=(
        "Force `modal deploy --strategy=rolling`: prior-version containers stay "
        "alive serving in-flight requests until they idle out. Mutually exclusive "
        "with --hard. Zero-downtime but a stale-content window where new code is "
        "deployed yet not actually serving traffic for several minutes."
    ),
)
@click.pass_context
def env_deploy(
    ctx: click.Context,
    yes_i_mean_production: bool,
    yes_i_mean_staging: bool,
    hard: bool,
    soft: bool,
) -> None:
    """Provision or upgrade the currently-activated env.

    Refuses when a recover-target file exists at the monorepo root.
    Refuses unless the shell is *deploy-activated* (i.e. ``MODAL_PROFILE``
    matches the tier's ``modal_workspace``); the refusal points the
    operator at ``eval "$(uv run minds env activate --deploy <name>)"``.

    Reads the activated env from ``MINDS_ROOT_NAME`` (set by
    ``minds env activate``) and dispatches:

    - ``production`` / ``staging``: pushes tier-shared Vault secrets to
      Modal and ``modal deploy``s both apps into the tier's stable Modal
      env. Writes nothing to disk. Requires
      ``--yes-i-mean-production`` (or ``--yes-i-mean-staging``) so an
      accidental invocation can never silently fire.
    - Anything else: per-env-tier deploy (``dev`` for ``dev-<user>``
      envs, ``ci`` for ``ci-<...>`` envs minted by the deployment-tests
      orchestrator) -- provisions the Modal env, Neon DB, SuperTokens
      app, pushes per-env Modal Secrets, deploys both apps, and writes
      ``~/.minds-<name>/client.toml`` + ``secrets.toml``.

    Idempotent: re-running picks up any new tier-shared Vault values and
    re-deploys in place.

    When neither ``--hard`` nor ``--soft`` is passed, the deploy strategy
    is chosen per :func:`resolve_deploy_strategy`: ``RECREATE`` whenever
    a migration ran or the tier is ``dev`` or ``ci`` (the per-env tiers
    -- personal dev envs and CI ephemeral envs respectively), ``ROLLOVER``
    for shared tiers with no migration (staging / production prefer
    zero-downtime when nothing risky happened).
    """
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)
    env_name = require_activated_env_name()
    tier = _tier_for_env_name(env_name)
    require_deploy_mode_activation(env_name=env_name, tier=tier)
    _refuse_if_this_env_recover_target_exists(env_name)

    if tier == _PRODUCTION_ENV_NAME and not yes_i_mean_production:
        raise click.ClickException(
            "Refusing to deploy against tier 'production' without --yes-i-mean-production. Pass that flag to confirm."
        )
    if tier == _STAGING_ENV_NAME and not yes_i_mean_staging:
        raise click.ClickException(
            "Refusing to deploy against tier 'staging' without --yes-i-mean-staging. Pass that flag to confirm."
        )

    if hard and soft:
        raise click.ClickException(
            "--hard and --soft are mutually exclusive: pass at most one to force the "
            "Modal deploy strategy, or omit both to let the default policy pick."
        )
    if hard:
        explicit_strategy: DeployStrategy | None = DeployStrategy.RECREATE
    elif soft:
        explicit_strategy = DeployStrategy.ROLLOVER
    else:
        explicit_strategy = None

    try:
        deploy_config = load_deploy_config(tier)
    except EnvConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    # Preflight the "must run from monorepo" check up front, BEFORE
    # reading vault credentials or building providers. ``deploy_env``
    # also re-checks (defense in depth + clean exception surface for
    # callers that bypass the CLI), but the CLI calling early means a
    # cd'd-into-/tmp invocation fails immediately with a clean error
    # rather than wasting a Vault round-trip first.
    try:
        find_monorepo_root()
    except MindError as exc:
        raise click.ClickException(str(exc)) from exc

    providers = _build_real_providers()

    with ConcurrencyGroup(name=f"minds-env-deploy-{env_name}") as cg:
        # Unified deploy_env runs the same code path for every tier and
        # picks per-step behavior off ``deploy_config.lifecycle``.
        # ``ProviderCredentials`` is loaded from Vault for every tier --
        # tiers without ``creates_resources`` simply don't consult the
        # neon_org_id / supertokens fields.
        try:
            credentials = _load_dev_credentials_from_vault(str(deploy_config.vault_path_prefix), cg=cg)
        except VaultReadError as exc:
            raise click.ClickException(str(exc)) from exc
        try:
            result = deploy_env(
                DevEnvName(env_name),
                tier=tier,
                deploy_config=deploy_config,
                providers=providers,
                parent_concurrency_group=cg,
                credentials=credentials,
                explicit_strategy=explicit_strategy,
            )
        except MindError as exc:
            # If a recover-target file was written before this failure
            # fired, automatically chain into `minds env recover` -- the
            # operator's whole point in using recover is to converge back
            # to the pre-deploy state, and forcing them to copy-paste a
            # follow-up command on every failure is a footgun.
            try:
                repo_root_for_recover = find_monorepo_root()
            except NotInMonorepoError:
                repo_root_for_recover = None
            if repo_root_for_recover is not None and recover_target_exists(
                repo_root=repo_root_for_recover, env_name=env_name
            ):
                _exec_into_recover(deploy_error=exc)
            raise click.ClickException(str(exc)) from exc
        _emit_deploy_result(result, output_format=output_format)


@env.command("destroy")
@click.option(
    "--keep-agents",
    is_flag=True,
    default=False,
    help=(
        "Forward-compatible flag for the eventual `mngr destroy` integration. "
        "Agent teardown is not yet implemented; running workspace agents are "
        "left alone today regardless of this flag (a warning is logged when "
        "the flag is omitted). Only consulted for dev-env destroys."
    ),
)
@click.option(
    "--yes-i-mean-staging",
    is_flag=True,
    default=False,
    help=(
        "Required confirmation for destroying the staging tier. Refuses without it "
        "so an accidental `minds env destroy` while activated against staging can "
        "never silently fire. Staging destroy `modal app stop`s both Modal apps and "
        "removes ~/.minds-staging/ -- Modal Secrets / Neon / SuperTokens stay (those "
        "are operator-managed). Production destroy is hard-refused regardless."
    ),
)
@click.pass_context
def env_destroy(ctx: click.Context, keep_agents: bool, yes_i_mean_staging: bool) -> None:
    """Tear down every resource ``minds env deploy`` provisioned for the activated env.

    Refuses hard-coded when no env is activated. Refuses hard-coded when
    the activated env is ``production`` (production teardown is
    operator-managed outside this CLI). ``staging`` requires
    ``--yes-i-mean-staging``. Also refuses unless the shell is
    *deploy-activated* (see :func:`env_deploy` for the same gate).

    The same destroy flow runs for every env type (see
    :func:`provisioning.destroy_env`). The only branches are the
    resource-management operations that genuinely differ by tier (dev
    deletes its own per-env Modal env / Neon DB / SuperTokens app
    outright; shared tiers wipe data inside operator-managed shared
    resources) and the generation-id removal (only for shared tiers
    that use generation tracking).
    """
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)
    env_name = require_activated_env_name()
    tier = _tier_for_env_name(env_name)
    require_deploy_mode_activation(env_name=env_name, tier=tier)
    _refuse_if_this_env_recover_target_exists(env_name)

    if tier == _PRODUCTION_ENV_NAME:
        raise click.ClickException(
            "Refusing to destroy production. Production tier teardown is operator-managed outside this CLI."
        )
    if tier == _STAGING_ENV_NAME and not yes_i_mean_staging:
        raise click.ClickException(
            "Refusing to destroy the staging tier without --yes-i-mean-staging. Pass that flag to confirm."
        )

    try:
        deploy_config = load_deploy_config(tier)
    except EnvConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    providers = _build_real_providers()

    with ConcurrencyGroup(name=f"minds-env-destroy-{env_name}") as cg:
        try:
            credentials = _load_dev_credentials_from_vault(str(deploy_config.vault_path_prefix), cg=cg)
        except VaultReadError as exc:
            raise click.ClickException(str(exc)) from exc

        # Tear down the env's unleased pool slices on their bare-metal boxes BEFORE
        # destroy_env deletes the per-env DB (after which the slice rows -- and thus
        # the only record of which VMs to destroy -- are gone). Leased slices are torn
        # down by destroy_env's agent-release path, so this targets only the baked pool
        # backlog that would otherwise leak its VMs on shared boxes. Must-succeed: a
        # box we cannot reach raises and stops the destroy rather than leaking.
        tear_down_env_pool_slices(env_name)

        try:
            destroy_env(
                DevEnvName(env_name),
                tier=tier,
                deploy_config=deploy_config,
                credentials=credentials,
                providers=providers,
                parent_concurrency_group=cg,
                keep_agents=keep_agents,
            )
        except DevEnvNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc
        except MindError as exc:
            logger.error("Destroy of {!r} failed: {}", env_name, exc)
            raise click.ClickException(str(exc)) from exc

    _emit_destroy_result(env_name, output_format=output_format)


@env.command("recover")
@click.option(
    "--from-failed-deploy",
    "is_from_failed_deploy",
    is_flag=True,
    hidden=True,
    help=(
        "Internal: set when `minds env deploy` auto-chains (execs) into recover after a "
        "failed deploy. Forces a non-zero exit even when the rollback itself succeeds, so "
        "the failed deploy is never reported to a caller / CI as success."
    ),
)
@click.pass_context
def env_recover(_ctx: click.Context, is_from_failed_deploy: bool) -> None:
    """Roll back to the pre-deploy state captured by a failed `minds env deploy`.

    Reads ``.minds-deploy-recover-target-<env-name>.json`` at the
    monorepo root for the currently-activated env, runs every reversal
    step (modal app rollback, Neon restore, orphan secret cleanup) in
    order, then deletes the file. Each step is idempotent so re-running
    recover after a partial recovery converges.

    Refuses to run if no recover-target file exists for the activated
    env. To recover a different env, activate it first. Also refuses
    unless the shell is *deploy-activated* (see :func:`env_deploy`).

    With ``--from-failed-deploy`` (set only by the deploy auto-rollback
    path), a *successful* rollback still exits non-zero -- the deploy
    that triggered it failed, and the exec'd recover owns the exit code
    the operator's shell / CI sees.
    """
    try:
        repo_root = find_monorepo_root()
    except MindError as exc:
        raise click.ClickException(str(exc)) from exc
    env_name = require_activated_env_name()
    require_deploy_mode_activation(env_name=env_name, tier=_tier_for_env_name(env_name))
    if not recover_target_exists(repo_root=repo_root, env_name=env_name):
        # Help the operator if they have recover-target files for OTHER
        # envs sitting around (a common mistake when juggling several
        # dev envs).
        leftovers = find_all_recover_target_files(repo_root=repo_root)
        if leftovers:
            leftover_list = "\n".join(f"  - {f.name}" for f in leftovers)
            raise click.ClickException(
                f"No recover-target file for activated env {env_name!r} at "
                f"{recover_target_path(repo_root=repo_root, env_name=env_name)}. Other recover-target files exist:\n"
                f"{leftover_list}\nActivate the env you want to recover (e.g. "
                f'`eval "$(uv run minds env activate --deploy <env-name>)"`) and re-run.'
            )
        raise click.ClickException(
            f"No recover-target file at {recover_target_path(repo_root=repo_root, env_name=env_name)}; "
            "nothing to recover. (Recover is only meaningful after a failed `minds env deploy`.)"
        )

    providers = _build_real_providers()
    with ConcurrencyGroup(name="minds-env-recover") as cg:
        # Load credentials from the tier-vault prefix the recover-target
        # file recorded. We re-derive the tier here since the recover-
        # target has env_name + tier already.
        target = read_recover_target(repo_root=repo_root, env_name=env_name)
        try:
            credentials = _load_dev_credentials_from_vault(target.vault_path_prefix, cg=cg)
        except VaultReadError as exc:
            raise click.ClickException(str(exc)) from exc
        try:
            recover_env(
                repo_root=repo_root,
                env_name=env_name,
                providers=providers,
                credentials=credentials,
                parent_cg=cg,
            )
        except RecoverTargetMissingError as exc:
            raise click.ClickException(str(exc)) from exc
        except RecoverFailedError as exc:
            raise click.ClickException(str(exc)) from exc

    # The rollback succeeded. When recover was auto-chained from a failed
    # deploy, the overall command still represents a FAILED deploy, so exit
    # non-zero -- otherwise a successful rollback would mask the deploy
    # failure from any caller / CI reading the exit code.
    if is_from_failed_deploy:
        raise click.ClickException(
            "Deploy failed; rolled back to the pre-deploy state. Exiting non-zero to reflect "
            "the deploy failure (the rollback itself succeeded)."
        )
